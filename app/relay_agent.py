"""Outbound Relay Agent so client PCs need zero tunnel/port-forwarding setup."""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import urlparse

import pyperclip
import websockets

from app.capture import ScreenCapturer
from app.config import SgDeskConfig, normalize_desk_id
from app.cursor_overlay import RemoteCursorOverlay
from app.elevation import is_admin, request_self_elevation
from app.input_win32 import (
    key_event,
    mouse_button,
    mouse_wheel,
    move_mouse,
    norm_to_screen_px,
    send_hotkey,
)


def _to_ws_base(http_url: str) -> str:
    url = http_url.strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://", "ws://", "wss://")):
        url = f"https://{url}"
    parsed = urlparse(url)
    scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
    return f"{scheme}://{parsed.netloc}"


class RelayAgent:
    """Maintains an outbound WebSocket connection from this Host PC to a central sgDesk Relay."""

    def __init__(
        self,
        cfg: SgDeskConfig,
        capturer: ScreenCapturer,
        on_auth_request: Callable[[str, str, str], Coroutine[Any, Any, str]],
        on_session_change: Callable[[], Coroutine[Any, Any, None]],
        cursor_overlay: Optional[RemoteCursorOverlay] = None,
        on_shutdown_request: Optional[Callable[[], None]] = None,
    ) -> None:
        self.cfg = cfg
        self.capturer = capturer
        self.on_auth_request = on_auth_request
        self.on_session_change = on_session_change
        self.cursor_overlay = cursor_overlay
        self.on_shutdown_request = on_shutdown_request

        self.status: str = "disabled"  # disabled | connecting | connected | error
        self.last_error: str = ""
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.relay_sessions: dict[str, dict[str, Any]] = {}

    def start_or_restart(self) -> None:
        self.stop()
        if not self.cfg.relay_url.strip():
            self.status = "disabled"
            self.last_error = ""
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())

    def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        if not self.cfg.relay_url.strip():
            self.status = "disabled"

    async def kick_session(self, session_id: str) -> None:
        sess = self.relay_sessions.get(session_id)
        if sess and sess.get("ws"):
            try:
                await sess["ws"].close()
            except Exception:
                pass

    async def toggle_input(self, session_id: str) -> None:
        sess = self.relay_sessions.get(session_id)
        if not sess:
            return
        sess["allow_input"] = not sess["allow_input"]
        ws = sess.get("ws")
        if ws:
            try:
                await ws.send(
                    json.dumps({"type": "input_permission", "allow_input": sess["allow_input"]})
                )
            except Exception:
                pass

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            ws_base = _to_ws_base(self.cfg.relay_url)
            if not ws_base:
                self.status = "disabled"
                return

            self.status = "connecting"
            self.last_error = ""
            await self.on_session_change()

            try:
                async with websockets.connect(
                    f"{ws_base}/ws/relay/host",
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "register",
                                "desk_id": normalize_desk_id(self.cfg.desk_id),
                                "hostname": self.cfg.hostname,
                            }
                        )
                    )
                    self.status = "connected"
                    self.last_error = ""
                    await self.on_session_change()

                    async for raw in ws:
                        msg = json.loads(raw)
                        mtype = msg.get("type")
                        if mtype == "auth_req":
                            asyncio.create_task(self._handle_auth_req(ws, msg))
                        elif mtype == "open_stream":
                            asyncio.create_task(
                                self._run_stream(
                                    ws_base,
                                    msg["channel_id"],
                                    msg.get("client_name", "Remote Viewer"),
                                    msg.get("remote_ip", "relay"),
                                )
                            )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.status = "error"
                self.last_error = str(exc)[:180]
                await self.on_session_change()

            if not self._stop.is_set():
                await asyncio.sleep(4.0)

    async def _handle_auth_req(self, ws: Any, msg: dict[str, Any]) -> None:
        req_id = msg["req_id"]
        secret = (msg.get("secret") or "").strip()
        client_name = msg.get("client_name", "Remote Viewer")
        remote_ip = msg.get("remote_ip", "relay")

        if secret:
            if self.cfg.verify_secret(secret):
                await ws.send(json.dumps({"type": "auth_res", "req_id": req_id, "status": "approved"}))
            else:
                await ws.send(json.dumps({"type": "auth_res", "req_id": req_id, "status": "invalid"}))
            return

        if not self.cfg.require_approval:
            await ws.send(json.dumps({"type": "auth_res", "req_id": req_id, "status": "approved"}))
            return

        decision = await self.on_auth_request(req_id, client_name, remote_ip)
        await ws.send(json.dumps({"type": "auth_res", "req_id": req_id, "status": decision}))

    async def _run_stream(
        self, ws_base: str, channel_id: str, client_name: str, remote_ip: str
    ) -> None:
        session_id = f"rel_{secrets.token_hex(4)}"
        monitors = self.capturer.get_monitors()
        default_mon = 1 if len(monitors) > 1 else 0

        sess: dict[str, Any] = {
            "session_id": session_id,
            "client_name": f"{client_name} (Relay)",
            "remote_ip": remote_ip,
            "connected_at": time.time(),
            "allow_input": self.cfg.allow_input,
            "monitor": default_mon,
            "quality": self.cfg.default_quality,
            "fps": self.cfg.default_fps,
            "scale": self.cfg.default_scale,
            "ws": None,
        }
        self.relay_sessions[session_id] = sess
        await self.on_session_change()

        stop_stream = asyncio.Event()
        pressed_buttons: set[str] = set()
        last_norm_pos: Optional[tuple[float, float]] = None

        try:
            async with websockets.connect(
                f"{ws_base}/ws/relay/stream/{channel_id}?role=host",
                ping_interval=20,
                ping_timeout=20,
                max_size=16 * 1024 * 1024,
            ) as ws:
                sess["ws"] = ws
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "session_id": session_id,
                            "desk_id": self.cfg.desk_id,
                            "hostname": self.cfg.hostname,
                            "allow_input": self.cfg.allow_input and sess["allow_input"],
                            "monitor": sess["monitor"],
                            "quality": sess["quality"],
                            "fps": sess["fps"],
                            "scale": sess["scale"],
                            "monitors": [
                                {
                                    "index": m.index,
                                    "name": m.name,
                                    "width": m.width,
                                    "height": m.height,
                                }
                                for m in monitors
                            ],
                        }
                    )
                )

                async def capture_loop() -> None:
                    prev_raw: Optional[bytes] = None
                    last_dims = (0, 0, -1)
                    last_sent_time = 0.0
                    try:
                        while not stop_stream.is_set():
                            t0 = time.perf_counter()
                            force = (t0 - last_sent_time) > 2.5
                            try:
                                jpeg, w, h, _, new_raw = await asyncio.to_thread(
                                    self.capturer.capture_jpeg,
                                    sess["monitor"],
                                    sess["quality"],
                                    sess["scale"],
                                    None if force else prev_raw,
                                )
                            except Exception:
                                await asyncio.sleep(0.3)
                                continue

                            if jpeg is not None:
                                prev_raw = new_raw
                                if (w, h, sess["monitor"]) != last_dims:
                                    last_dims = (w, h, sess["monitor"])
                                    await ws.send(
                                        json.dumps(
                                            {
                                                "type": "frame_meta",
                                                "width": w,
                                                "height": h,
                                                "monitor": sess["monitor"],
                                            }
                                        )
                                    )
                                await ws.send(jpeg)
                                last_sent_time = time.perf_counter()

                            interval = 1.0 / max(1, sess["fps"])
                            elapsed = time.perf_counter() - t0
                            await asyncio.sleep(max(0.004, interval - elapsed))
                    except Exception:
                        stop_stream.set()

                cap_task = asyncio.create_task(capture_loop())
                try:
                    async for raw_msg in ws:
                        if isinstance(raw_msg, bytes):
                            continue
                        msg = json.loads(raw_msg)
                        mtype = msg.get("type")

                        if mtype == "ping":
                            await ws.send(json.dumps({"type": "pong", "t": msg.get("t")}))
                            continue
                        if mtype == "config":
                            if "quality" in msg:
                                sess["quality"] = max(10, min(95, int(msg["quality"])))
                            if "fps" in msg:
                                sess["fps"] = max(1, min(60, int(msg["fps"])))
                            if "scale" in msg:
                                sess["scale"] = max(0.25, min(1.0, float(msg["scale"])))
                            if "monitor" in msg:
                                sess["monitor"] = int(msg["monitor"])
                            continue
                        if mtype == "clipboard_get":
                            text = await asyncio.to_thread(pyperclip.paste)
                            await ws.send(json.dumps({"type": "clipboard", "text": text or ""}))
                            continue
                        if mtype == "request_elevation":
                            if is_admin():
                                await ws.send(
                                    json.dumps(
                                        {
                                            "type": "elevation_status",
                                            "elevated": True,
                                            "restarting": False,
                                            "message": "Host is already running as Administrator.",
                                        }
                                    )
                                )
                            else:
                                ok = await asyncio.to_thread(request_self_elevation, sys.argv[1:])
                                if ok:
                                    await ws.send(
                                        json.dumps(
                                            {
                                                "type": "elevation_status",
                                                "elevated": False,
                                                "restarting": True,
                                                "message": "Elevation granted! Host is restarting as Administrator...",
                                            }
                                        )
                                    )
                                    await asyncio.sleep(0.35)
                                    if self.on_shutdown_request:
                                        self.on_shutdown_request()
                                else:
                                    await ws.send(
                                        json.dumps(
                                            {
                                                "type": "elevation_status",
                                                "elevated": False,
                                                "restarting": False,
                                                "message": "Elevation request was cancelled or denied.",
                                            }
                                        )
                                    )
                            continue

                        if not (self.cfg.allow_input and sess["allow_input"]):
                            continue

                        if mtype == "clipboard_set":
                            await asyncio.to_thread(pyperclip.copy, str(msg.get("text", "")))
                        elif mtype == "mouse_move":
                            mon_rect = self.capturer.get_monitor_rect(sess["monitor"])
                            last_norm_pos = (msg["x"], msg["y"])
                            if self.cursor_overlay:
                                px, py = norm_to_screen_px(msg["x"], msg["y"], mon_rect)
                                self.cursor_overlay.update_cursor(session_id, client_name, px, py)
                            if pressed_buttons:
                                await asyncio.to_thread(move_mouse, msg["x"], msg["y"], mon_rect)
                        elif mtype in ("mouse_down", "mouse_up"):
                            mon_rect = self.capturer.get_monitor_rect(sess["monitor"])
                            btn = str(msg.get("button", "left"))
                            action = "down" if mtype == "mouse_down" else "up"
                            if action == "down":
                                pressed_buttons.add(btn)
                            else:
                                pressed_buttons.discard(btn)
                            last_norm_pos = (msg["x"], msg["y"])
                            if self.cursor_overlay:
                                px, py = norm_to_screen_px(msg["x"], msg["y"], mon_rect)
                                self.cursor_overlay.update_cursor(session_id, client_name, px, py)
                            await asyncio.to_thread(
                                mouse_button,
                                btn,
                                action,
                                msg["x"],
                                msg["y"],
                                mon_rect,
                            )
                        elif mtype == "wheel":
                            if last_norm_pos is not None:
                                mon_rect = self.capturer.get_monitor_rect(sess["monitor"])
                                await asyncio.to_thread(
                                    move_mouse, last_norm_pos[0], last_norm_pos[1], mon_rect
                                )
                            await asyncio.to_thread(
                                mouse_wheel, float(msg.get("dx", 0)), float(msg.get("dy", 0))
                            )
                        elif mtype == "key":
                            await asyncio.to_thread(
                                key_event, str(msg.get("code", "")), str(msg.get("action", "down"))
                            )
                        elif mtype == "hotkey":
                            keys = msg.get("keys")
                            if isinstance(keys, list):
                                await asyncio.to_thread(send_hotkey, [str(k) for k in keys])
                finally:
                    stop_stream.set()
                    cap_task.cancel()
        except Exception:
            pass
        finally:
            if self.cursor_overlay:
                self.cursor_overlay.remove_cursor(session_id)
            self.relay_sessions.pop(session_id, None)
            await self.on_session_change()
