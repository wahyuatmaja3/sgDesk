"""FastAPI & WebSocket server for sgDesk."""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import psutil
import pyperclip
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import __version__
from app.capture import ScreenCapturer
from app.config import SgDeskConfig, normalize_desk_id
from app.cursor_overlay import RemoteCursorOverlay
from app.input_win32 import (
    key_event,
    mouse_button,
    mouse_wheel,
    move_mouse,
    norm_to_screen_px,
    send_hotkey,
)
from app.relay_agent import RelayAgent
from app.tunnel import CloudflareTunnelManager

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    STATIC_DIR = Path(sys._MEIPASS) / "static"
else:
    STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

PROXY_HEADERS = {
    "cf-connecting-ip",
    "cf-ray",
    "x-forwarded-for",
    "x-forwarded-host",
    "forwarded",
}


@dataclass
class PendingRequest:
    request_id: str
    client_name: str
    remote_ip: str
    created_at: float
    status: str = "pending"  # pending | approved | rejected
    session_token: str = ""


@dataclass
class ActiveSession:
    session_id: str
    token: str
    client_name: str
    remote_ip: str
    connected_at: float
    allow_input: bool = True
    monitor: int = 1
    quality: int = 75
    fps: int = 24
    scale: float = 1.0
    ws: Optional[WebSocket] = field(default=None, repr=False)


class AuthBody(BaseModel):
    secret: str
    client_name: str = "Remote Viewer"


class RelayConnectBody(BaseModel):
    desk_id: str
    secret: str = ""
    client_name: str = "Remote Viewer"


class HostSettingsBody(BaseModel):
    unattended_password: Optional[str] = None
    clear_unattended_password: bool = False
    require_approval: Optional[bool] = None
    allow_input: Optional[bool] = None
    tunnel_url: Optional[str] = None
    relay_url: Optional[str] = None
    auto_quick_tunnel: Optional[bool] = None
    default_quality: Optional[int] = None
    default_fps: Optional[int] = None
    default_scale: Optional[float] = None


class DecisionBody(BaseModel):
    approve: bool


def create_app(cfg: SgDeskConfig) -> FastAPI:
    capturer = ScreenCapturer()
    cursor_overlay = RemoteCursorOverlay()
    main_loop: Optional[asyncio.AbstractEventLoop] = None

    def _on_tunnel_update() -> None:
        if main_loop and not main_loop.is_closed():
            main_loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    broadcast_host_event({"type": "state", "data": build_host_state()})
                )
            )

    tunnel_mgr = CloudflareTunnelManager(on_update=_on_tunnel_update)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal main_loop
        loop = asyncio.get_running_loop()
        main_loop = loop
        orig_handler = loop.get_exception_handler()

        def _quiet_proactor_reset(lp: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            exc = context.get("exception")
            if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
                return
            if orig_handler is not None:
                orig_handler(lp, context)
            else:
                lp.default_exception_handler(context)

        loop.set_exception_handler(_quiet_proactor_reset)
        relay_agent.start_or_restart()
        if (
            cfg.auto_quick_tunnel
            and not cfg.tunnel_url.strip()
            and not tunnel_mgr.status().system_worker_running
        ):
            tunnel_mgr.start_quick_tunnel(cfg.port)
        try:
            yield
        finally:
            relay_agent.stop()
            tunnel_mgr.stop_quick_tunnel()
            cursor_overlay.stop()

    app = FastAPI(title="sgDesk", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth & session state
    approved_tokens: dict[str, dict[str, Any]] = {}
    pending_requests: dict[str, PendingRequest] = {}
    pending_futures: dict[str, asyncio.Future] = {}
    active_sessions: dict[str, ActiveSession] = {}
    host_ws_clients: set[WebSocket] = set()

    # Central Relay Server state
    relay_hosts: dict[str, dict[str, Any]] = {}
    relay_tokens: dict[str, dict[str, Any]] = {}
    relay_channels: dict[str, dict[str, Any]] = {}

    async def handle_relay_auth_prompt(req_id: str, client_name: str, remote_ip: str) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending_futures[req_id] = fut
        pending_requests[req_id] = PendingRequest(
            request_id=req_id,
            client_name=f"{client_name} (Relay)",
            remote_ip=remote_ip,
            created_at=time.time(),
        )
        await broadcast_host_event({"type": "state", "data": build_host_state()})
        try:
            return await asyncio.wait_for(fut, timeout=85.0)
        except asyncio.TimeoutError:
            pending_requests.pop(req_id, None)
            await broadcast_host_event({"type": "state", "data": build_host_state()})
            return "rejected"
        finally:
            pending_futures.pop(req_id, None)

    async def on_relay_session_change() -> None:
        await broadcast_host_event({"type": "state", "data": build_host_state()})

    relay_agent = RelayAgent(
        cfg=cfg,
        capturer=capturer,
        on_auth_request=handle_relay_auth_prompt,
        on_session_change=on_relay_session_change,
        cursor_overlay=cursor_overlay,
    )

    def get_client_ip(request_or_ws: Request | WebSocket) -> str:
        cf_ip = request_or_ws.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        xff = request_or_ws.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        if request_or_ws.client:
            return request_or_ws.client.host
        return "unknown"

    def is_local_admin(request_or_ws: Request | WebSocket) -> bool:
        headers = request_or_ws.headers
        token = headers.get("x-sgdesk-admin") or request_or_ws.query_params.get("admin_token")
        if token and secrets.compare_digest(token, cfg.host_admin_token):
            return True

        # Reject if behind Cloudflare Tunnel or any reverse proxy
        if any(h in headers for h in PROXY_HEADERS):
            return False

        host_hdr = (headers.get("host") or "").lower().split(":")[0]
        if host_hdr not in ("localhost", "127.0.0.1", "::1"):
            return False

        client_host = request_or_ws.client.host if request_or_ws.client else ""
        return client_host in ("127.0.0.1", "::1", "localhost")

    def require_local_admin(request: Request) -> None:
        if not is_local_admin(request):
            raise HTTPException(status_code=403, detail="Host admin access is restricted to localhost.")

    async def broadcast_host_event(event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        payload = json.dumps(event)
        for ws in list(host_ws_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            host_ws_clients.discard(ws)

    def build_host_state() -> dict[str, Any]:
        t_stat = tunnel_mgr.status()
        monitors = [
            {
                "index": m.index,
                "name": m.name,
                "width": m.width,
                "height": m.height,
                "is_primary": m.is_primary,
            }
            for m in capturer.get_monitors()
        ]
        vm = psutil.virtual_memory()
        return {
            "version": __version__,
            "desk_id": cfg.desk_id,
            "hostname": cfg.hostname,
            "port": cfg.port,
            "session_pin": cfg.session_pin,
            "has_unattended_password": bool(cfg.unattended_password),
            "require_approval": cfg.require_approval,
            "allow_input": cfg.allow_input,
            "default_quality": cfg.default_quality,
            "default_fps": cfg.default_fps,
            "default_scale": cfg.default_scale,
            "tunnel_url": cfg.tunnel_url,
            "relay_url": cfg.relay_url,
            "auto_quick_tunnel": cfg.auto_quick_tunnel,
            "relay_status": relay_agent.status,
            "relay_error": relay_agent.last_error,
            "relay_online_desks": len(relay_hosts),
            "admin_token": cfg.host_admin_token,
            "monitors": monitors,
            "tunnel_status": {
                "cloudflared_installed": t_stat.cloudflared_installed,
                "cloudflared_version": t_stat.cloudflared_version,
                "system_worker_running": t_stat.system_worker_running,
                "quick_tunnel_running": t_stat.quick_tunnel_running,
                "quick_tunnel_url": t_stat.quick_tunnel_url,
                "quick_tunnel_error": t_stat.quick_tunnel_error,
                "downloading": t_stat.downloading,
            },
            "system_stats": {
                "cpu_percent": psutil.cpu_percent(interval=None),
                "ram_percent": vm.percent,
            },
            "active_sessions": [
                {
                    "session_id": s.session_id,
                    "client_name": s.client_name,
                    "remote_ip": s.remote_ip,
                    "connected_at": s.connected_at,
                    "allow_input": s.allow_input,
                    "monitor": s.monitor,
                }
                for s in active_sessions.values()
            ]
            + [
                {
                    "session_id": s["session_id"],
                    "client_name": s["client_name"],
                    "remote_ip": s["remote_ip"],
                    "connected_at": s["connected_at"],
                    "allow_input": s["allow_input"],
                    "monitor": s["monitor"],
                }
                for s in relay_agent.relay_sessions.values()
            ],
            "pending_requests": [
                {
                    "request_id": r.request_id,
                    "client_name": r.client_name,
                    "remote_ip": r.remote_ip,
                    "created_at": r.created_at,
                    "status": r.status,
                }
                for r in pending_requests.values()
                if r.status == "pending"
            ],
        }

    # --- Public endpoints ---

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/public/info")
    async def public_info(request: Request) -> JSONResponse:
        local = is_local_admin(request)
        return JSONResponse(
            {
                "app": "sgDesk",
                "version": __version__,
                "desk_id": cfg.desk_id,
                "hostname": cfg.hostname,
                "is_local_host": local,
                "require_approval": cfg.require_approval,
            }
        )

    @app.post("/api/auth")
    async def authenticate(body: AuthBody, request: Request) -> JSONResponse:
        if not cfg.verify_secret(body.secret.strip()):
            raise HTTPException(status_code=401, detail="Invalid Access PIN or Password.")

        client_ip = get_client_ip(request)
        client_name = (body.client_name or "Remote Viewer").strip()[:48]

        if not cfg.require_approval:
            token = secrets.token_urlsafe(24)
            approved_tokens[token] = {
                "client_name": client_name,
                "remote_ip": client_ip,
                "created_at": time.time(),
            }
            return JSONResponse({"status": "approved", "token": token})

        req_id = secrets.token_urlsafe(12)
        pending = PendingRequest(
            request_id=req_id,
            client_name=client_name,
            remote_ip=client_ip,
            created_at=time.time(),
        )
        pending_requests[req_id] = pending
        await broadcast_host_event({"type": "state", "data": build_host_state()})
        return JSONResponse({"status": "pending", "request_id": req_id})

    @app.get("/api/auth/status/{request_id}")
    async def auth_status(request_id: str) -> JSONResponse:
        req = pending_requests.get(request_id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found or expired.")
        if time.time() - req.created_at > 90 and req.status == "pending":
            req.status = "rejected"
        if req.status == "approved":
            return JSONResponse({"status": "approved", "token": req.session_token})
        return JSONResponse({"status": req.status})

    # --- Host Admin endpoints (localhost only) ---

    @app.get("/api/host/state")
    async def host_state(request: Request) -> JSONResponse:
        require_local_admin(request)
        return JSONResponse(build_host_state())

    @app.post("/api/host/pin/regenerate")
    async def host_regenerate_pin(request: Request) -> JSONResponse:
        require_local_admin(request)
        new_pin = cfg.regenerate_pin()
        await broadcast_host_event({"type": "state", "data": build_host_state()})
        return JSONResponse({"session_pin": new_pin})

    @app.post("/api/host/settings")
    async def host_update_settings(body: HostSettingsBody, request: Request) -> JSONResponse:
        require_local_admin(request)
        if body.clear_unattended_password:
            cfg.unattended_password = ""
        elif body.unattended_password is not None and body.unattended_password != "":
            cfg.unattended_password = body.unattended_password
        if body.require_approval is not None:
            cfg.require_approval = body.require_approval
        if body.allow_input is not None:
            cfg.allow_input = body.allow_input
        if body.tunnel_url is not None:
            cfg.tunnel_url = body.tunnel_url.strip().rstrip("/")
        if body.relay_url is not None:
            new_relay = body.relay_url.strip().rstrip("/")
            if new_relay != cfg.relay_url:
                cfg.relay_url = new_relay
                relay_agent.start_or_restart()
        if body.auto_quick_tunnel is not None:
            cfg.auto_quick_tunnel = body.auto_quick_tunnel
            if (
                cfg.auto_quick_tunnel
                and not tunnel_mgr.status().quick_tunnel_running
                and not tunnel_mgr.status().system_worker_running
            ):
                tunnel_mgr.start_quick_tunnel(cfg.port)
        if body.default_quality is not None:
            cfg.default_quality = max(10, min(95, body.default_quality))
        if body.default_fps is not None:
            cfg.default_fps = max(5, min(60, body.default_fps))
        if body.default_scale is not None:
            cfg.default_scale = max(0.25, min(1.0, body.default_scale))
        cfg.save()
        await broadcast_host_event({"type": "state", "data": build_host_state()})
        return JSONResponse(build_host_state())

    @app.post("/api/host/tunnel/quick/start")
    async def host_start_quick_tunnel(request: Request) -> JSONResponse:
        require_local_admin(request)
        tunnel_mgr.start_quick_tunnel(cfg.port)
        await asyncio.sleep(0.5)
        state = build_host_state()
        await broadcast_host_event({"type": "state", "data": state})
        return JSONResponse(state)

    @app.post("/api/host/tunnel/quick/stop")
    async def host_stop_quick_tunnel(request: Request) -> JSONResponse:
        require_local_admin(request)
        tunnel_mgr.stop_quick_tunnel()
        state = build_host_state()
        await broadcast_host_event({"type": "state", "data": state})
        return JSONResponse(state)

    @app.post("/api/host/requests/{request_id}/decide")
    async def host_decide_request(request_id: str, body: DecisionBody, request: Request) -> JSONResponse:
        require_local_admin(request)
        req = pending_requests.get(request_id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found.")
        if body.approve:
            token = secrets.token_urlsafe(24)
            req.status = "approved"
            req.session_token = token
            approved_tokens[token] = {
                "client_name": req.client_name,
                "remote_ip": req.remote_ip,
                "created_at": time.time(),
            }
        else:
            req.status = "rejected"
        fut = pending_futures.get(request_id)
        if fut and not fut.done():
            fut.set_result("approved" if body.approve else "rejected")
        state = build_host_state()
        await broadcast_host_event({"type": "state", "data": state})
        return JSONResponse(state)

    @app.post("/api/host/sessions/{session_id}/disconnect")
    async def host_disconnect_session(session_id: str, request: Request) -> JSONResponse:
        require_local_admin(request)
        if session_id in relay_agent.relay_sessions:
            await relay_agent.kick_session(session_id)
        else:
            sess = active_sessions.pop(session_id, None)
            if sess and sess.ws:
                try:
                    await sess.ws.close(code=4000, reason="Disconnected by host")
                except Exception:
                    pass
        state = build_host_state()
        await broadcast_host_event({"type": "state", "data": state})
        return JSONResponse(state)

    @app.post("/api/host/sessions/{session_id}/toggle_input")
    async def host_toggle_session_input(session_id: str, request: Request) -> JSONResponse:
        require_local_admin(request)
        if session_id in relay_agent.relay_sessions:
            await relay_agent.toggle_input(session_id)
        else:
            sess = active_sessions.get(session_id)
            if not sess:
                raise HTTPException(status_code=404, detail="Session not found.")
            sess.allow_input = not sess.allow_input
            if sess.ws:
                try:
                    await sess.ws.send_text(
                        json.dumps({"type": "input_permission", "allow_input": sess.allow_input})
                    )
                except Exception:
                    pass
        state = build_host_state()
        await broadcast_host_event({"type": "state", "data": state})
        return JSONResponse(state)

    # --- Host WebSocket ---

    @app.websocket("/ws/host")
    async def ws_host(websocket: WebSocket) -> None:
        if not is_local_admin(websocket):
            await websocket.close(code=4003)
            return
        await websocket.accept()
        host_ws_clients.add(websocket)
        try:
            await websocket.send_text(json.dumps({"type": "state", "data": build_host_state()}))
            while True:
                # Keepalive / periodic stats refresh
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=3.0)
                    if msg == "refresh":
                        await websocket.send_text(
                            json.dumps({"type": "state", "data": build_host_state()})
                        )
                except asyncio.TimeoutError:
                    await websocket.send_text(
                        json.dumps({"type": "state", "data": build_host_state()})
                    )
        except WebSocketDisconnect:
            pass
        finally:
            host_ws_clients.discard(websocket)

    # --- Remote Desk WebSocket ---

    @app.websocket("/ws/desk")
    async def ws_desk(websocket: WebSocket, token: str = "") -> None:
        meta = approved_tokens.get(token)
        if not meta:
            await websocket.close(code=4001, reason="Invalid or expired token")
            return

        await websocket.accept()
        session_id = secrets.token_hex(6)
        monitors = capturer.get_monitors()
        default_mon = 1 if len(monitors) > 1 else 0

        sess = ActiveSession(
            session_id=session_id,
            token=token,
            client_name=meta["client_name"],
            remote_ip=get_client_ip(websocket),
            connected_at=time.time(),
            allow_input=cfg.allow_input,
            monitor=default_mon,
            quality=cfg.default_quality,
            fps=cfg.default_fps,
            scale=cfg.default_scale,
            ws=websocket,
        )
        active_sessions[session_id] = sess
        await broadcast_host_event({"type": "state", "data": build_host_state()})

        # Send initial session handshake
        await websocket.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "session_id": session_id,
                    "desk_id": cfg.desk_id,
                    "hostname": cfg.hostname,
                    "allow_input": cfg.allow_input and sess.allow_input,
                    "monitor": sess.monitor,
                    "quality": sess.quality,
                    "fps": sess.fps,
                    "scale": sess.scale,
                    "monitors": [
                        {"index": m.index, "name": m.name, "width": m.width, "height": m.height}
                        for m in monitors
                    ],
                }
            )
        )

        stop_event = asyncio.Event()

        async def stream_loop() -> None:
            prev_raw: Optional[bytes] = None
            last_dims = (0, 0, -1)
            last_sent_time = 0.0
            try:
                while not stop_event.is_set():
                    t0 = time.perf_counter()
                    # Force keyframe at least every 2.5s if idle
                    force = (t0 - last_sent_time) > 2.5
                    try:
                        jpeg, w, h, _, new_raw = await asyncio.to_thread(
                            capturer.capture_jpeg,
                            sess.monitor,
                            sess.quality,
                            sess.scale,
                            None if force else prev_raw,
                        )
                    except Exception:
                        # Handle transient GDI/UAC/lock-screen capture errors without dropping session
                        await asyncio.sleep(0.3)
                        continue
                    if jpeg is not None:
                        prev_raw = new_raw
                        if (w, h, sess.monitor) != last_dims:
                            last_dims = (w, h, sess.monitor)
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "frame_meta",
                                        "width": w,
                                        "height": h,
                                        "monitor": sess.monitor,
                                    }
                                )
                            )
                        await websocket.send_bytes(jpeg)
                        last_sent_time = time.perf_counter()

                    interval = 1.0 / max(1, sess.fps)
                    elapsed = time.perf_counter() - t0
                    await asyncio.sleep(max(0.004, interval - elapsed))
            except Exception:
                stop_event.set()

        streamer = asyncio.create_task(stream_loop())
        pressed_buttons: set[str] = set()
        last_norm_pos: Optional[tuple[float, float]] = None

        try:
            while not stop_event.is_set():
                raw_msg = await websocket.receive_text()
                msg = json.loads(raw_msg)
                mtype = msg.get("type")

                if mtype == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "t": msg.get("t")}))
                    continue

                if mtype == "config":
                    if "quality" in msg:
                        sess.quality = max(10, min(95, int(msg["quality"])))
                    if "fps" in msg:
                        sess.fps = max(1, min(60, int(msg["fps"])))
                    if "scale" in msg:
                        sess.scale = max(0.25, min(1.0, float(msg["scale"])))
                    if "monitor" in msg:
                        sess.monitor = int(msg["monitor"])
                    continue

                if mtype == "clipboard_get":
                    text = await asyncio.to_thread(pyperclip.paste)
                    await websocket.send_text(json.dumps({"type": "clipboard", "text": text or ""}))
                    continue

                if not (cfg.allow_input and sess.allow_input):
                    continue

                if mtype == "clipboard_set":
                    await asyncio.to_thread(pyperclip.copy, str(msg.get("text", "")))
                elif mtype == "mouse_move":
                    mon_rect = capturer.get_monitor_rect(sess.monitor)
                    last_norm_pos = (msg["x"], msg["y"])
                    px, py = norm_to_screen_px(msg["x"], msg["y"], mon_rect)
                    cursor_overlay.update_cursor(session_id, sess.client_name, px, py)
                    if pressed_buttons:
                        await asyncio.to_thread(move_mouse, msg["x"], msg["y"], mon_rect)
                elif mtype in ("mouse_down", "mouse_up"):
                    mon_rect = capturer.get_monitor_rect(sess.monitor)
                    btn = str(msg.get("button", "left"))
                    action = "down" if mtype == "mouse_down" else "up"
                    if action == "down":
                        pressed_buttons.add(btn)
                    else:
                        pressed_buttons.discard(btn)
                    last_norm_pos = (msg["x"], msg["y"])
                    px, py = norm_to_screen_px(msg["x"], msg["y"], mon_rect)
                    cursor_overlay.update_cursor(session_id, sess.client_name, px, py)
                    await asyncio.to_thread(
                        mouse_button, btn, action, msg["x"], msg["y"], mon_rect
                    )
                elif mtype == "wheel":
                    if last_norm_pos is not None:
                        mon_rect = capturer.get_monitor_rect(sess.monitor)
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
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            stop_event.set()
            streamer.cancel()
            cursor_overlay.remove_cursor(session_id)
            active_sessions.pop(session_id, None)
            await broadcast_host_event({"type": "state", "data": build_host_state()})

    # --- Central Relay Server endpoints ---

    @app.get("/api/relay/check/{desk_id}")
    async def relay_check_desk(desk_id: str) -> JSONResponse:
        did = normalize_desk_id(desk_id)
        if did == normalize_desk_id(cfg.desk_id):
            return JSONResponse({"online": True, "desk_id": did, "hostname": cfg.hostname, "local": True})
        host = relay_hosts.get(did)
        if host:
            return JSONResponse({"online": True, "desk_id": did, "hostname": host["hostname"], "local": False})
        return JSONResponse({"online": False, "desk_id": did})

    @app.post("/api/relay/connect")
    async def relay_connect(body: RelayConnectBody, request: Request) -> JSONResponse:
        did = normalize_desk_id(body.desk_id)
        client_ip = get_client_ip(request)
        client_name = (body.client_name or "Remote Viewer").strip()[:48]
        secret = (body.secret or "").strip()

        # If target is this machine itself, delegate to local auth flow
        if did == normalize_desk_id(cfg.desk_id):
            if secret:
                if not cfg.verify_secret(secret):
                    raise HTTPException(status_code=401, detail="Invalid Access PIN or Password.")
                token = secrets.token_urlsafe(24)
                approved_tokens[token] = {
                    "client_name": client_name,
                    "remote_ip": client_ip,
                    "created_at": time.time(),
                }
                return JSONResponse({"status": "approved", "token": token, "ws_path": "/ws/desk"})

            if not cfg.require_approval:
                token = secrets.token_urlsafe(24)
                approved_tokens[token] = {
                    "client_name": client_name,
                    "remote_ip": client_ip,
                    "created_at": time.time(),
                }
                return JSONResponse({"status": "approved", "token": token, "ws_path": "/ws/desk"})
            req_id = secrets.token_urlsafe(12)
            pending_requests[req_id] = PendingRequest(
                request_id=req_id,
                client_name=client_name,
                remote_ip=client_ip,
                created_at=time.time(),
            )
            await broadcast_host_event({"type": "state", "data": build_host_state()})
            return JSONResponse({"status": "pending", "request_id": req_id, "ws_path": "/ws/desk"})

        host = relay_hosts.get(did)
        if not host:
            raise HTTPException(
                status_code=404,
                detail=f"Desk ID '{did}' is offline or not connected to this Relay.",
            )

        req_id = secrets.token_urlsafe(12)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        host["pending_auth"][req_id] = fut

        try:
            await host["ws"].send_text(
                json.dumps(
                    {
                        "type": "auth_req",
                        "req_id": req_id,
                        "secret": body.secret.strip(),
                        "client_name": client_name,
                        "remote_ip": client_ip,
                    }
                )
            )
            decision = await asyncio.wait_for(fut, timeout=90.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=408, detail="Timed out waiting for host approval.")
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to reach remote host.")
        finally:
            host["pending_auth"].pop(req_id, None)

        if decision == "invalid":
            raise HTTPException(status_code=401, detail="Invalid Access PIN or Password.")
        if decision != "approved":
            raise HTTPException(status_code=403, detail="Connection was rejected by host.")

        token = secrets.token_urlsafe(24)
        relay_tokens[token] = {
            "desk_id": did,
            "client_name": client_name,
            "remote_ip": client_ip,
            "created_at": time.time(),
        }
        return JSONResponse({"status": "approved", "token": token, "ws_path": "/ws/relay/view"})

    @app.websocket("/ws/relay/host")
    async def ws_relay_host(websocket: WebSocket) -> None:
        await websocket.accept()
        desk_id = ""
        try:
            reg_raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            reg = json.loads(reg_raw)
            if reg.get("type") != "register" or not reg.get("desk_id"):
                await websocket.close(code=4002)
                return
            desk_id = normalize_desk_id(str(reg["desk_id"]))
            hostname = str(reg.get("hostname") or desk_id)[:64]

            relay_hosts[desk_id] = {
                "ws": websocket,
                "desk_id": desk_id,
                "hostname": hostname,
                "connected_at": time.time(),
                "pending_auth": {},
            }
            await broadcast_host_event({"type": "state", "data": build_host_state()})

            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                if msg.get("type") == "auth_res":
                    req_id = msg.get("req_id")
                    fut = relay_hosts.get(desk_id, {}).get("pending_auth", {}).get(req_id)
                    if fut and not fut.done():
                        fut.set_result(msg.get("status", "rejected"))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if desk_id and relay_hosts.get(desk_id, {}).get("ws") is websocket:
                relay_hosts.pop(desk_id, None)
                await broadcast_host_event({"type": "state", "data": build_host_state()})

    @app.websocket("/ws/relay/view")
    async def ws_relay_view(websocket: WebSocket, token: str = "") -> None:
        meta = relay_tokens.pop(token, None)
        if not meta:
            await websocket.close(code=4001, reason="Invalid or expired relay token")
            return
        host = relay_hosts.get(meta["desk_id"])
        if not host:
            await websocket.close(code=4004, reason="Remote host disconnected")
            return

        await websocket.accept()
        channel_id = secrets.token_hex(8)
        ready_ev = asyncio.Event()
        relay_channels[channel_id] = {
            "viewer_ws": websocket,
            "host_ws": None,
            "ready": ready_ev,
        }

        try:
            await host["ws"].send_text(
                json.dumps(
                    {
                        "type": "open_stream",
                        "channel_id": channel_id,
                        "client_name": meta["client_name"],
                        "remote_ip": meta["remote_ip"],
                    }
                )
            )
            await asyncio.wait_for(ready_ev.wait(), timeout=12.0)
            host_ws: WebSocket = relay_channels[channel_id]["host_ws"]

            # Pipe viewer -> host
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await host_ws.send_text(msg["text"])
                elif msg.get("bytes") is not None:
                    await host_ws.send_bytes(msg["bytes"])
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            ch = relay_channels.pop(channel_id, None)
            if ch and ch.get("host_ws"):
                try:
                    await ch["host_ws"].close()
                except Exception:
                    pass

    @app.websocket("/ws/relay/stream/{channel_id}")
    async def ws_relay_stream(websocket: WebSocket, channel_id: str) -> None:
        ch = relay_channels.get(channel_id)
        if not ch or ch["host_ws"] is not None:
            await websocket.close(code=4004)
            return

        await websocket.accept()
        ch["host_ws"] = websocket
        ch["ready"].set()
        viewer_ws: WebSocket = ch["viewer_ws"]

        try:
            # Pipe host -> viewer
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await viewer_ws.send_bytes(msg["bytes"])
                elif msg.get("text") is not None:
                    await viewer_ws.send_text(msg["text"])
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            relay_channels.pop(channel_id, None)
            try:
                await viewer_ws.close()
            except Exception:
                pass

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
