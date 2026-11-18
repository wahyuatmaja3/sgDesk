"""sgDesk launcher - Remote Desktop powered by WebSockets & Cloudflare Tunnel."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

import pyperclip
import uvicorn

from app.config import CONFIG_DIR, SgDeskConfig
from app.elevation import is_admin, request_self_elevation, wait_for_pid_exit
from app.server import create_app
from app.tray import SystemTrayIcon


def _ensure_stdio() -> None:
    """Prevent crashes in PyInstaller --noconsole mode where stdout/stderr are None."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _find_chromium_app_browser() -> str | None:
    """Locate Edge or Chrome executable across standard Windows install paths."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(local_app, r"Google\Chrome\Application\chrome.exe") if local_app else "",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def open_app_window(url: str, delay: float = 1.0) -> None:
    """Open sgDesk in a standalone desktop app window (no browser tabs/address bar)."""
    if delay > 0:
        time.sleep(delay)
    browser_exe = _find_chromium_app_browser()
    if browser_exe:
        profile_dir = str(CONFIG_DIR / "ui_profile")
        try:
            subprocess.Popen(
                [
                    browser_exe,
                    f"--app={url}",
                    "--window-size=960,640",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]
            )
            return
        except Exception:
            pass
    webbrowser.open(url)


def main() -> None:
    _ensure_stdio()

    parser = argparse.ArgumentParser(description="sgDesk Remote Desktop")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on (default: 3737)")
    parser.add_argument(
        "--relay",
        type=str,
        default=None,
        help="Central sgDesk Relay URL (default: https://remote.step.my.id)",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Run as Central Relay Server (clears outbound relay URL)",
    )
    parser.add_argument("--no-browser", action="store_true", help="Run headless without opening UI")
    parser.add_argument("--no-tray", action="store_true", help="Disable system tray icon")
    parser.add_argument("--wait-pid", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.wait_pid:
        wait_for_pid_exit(args.wait_pid, timeout=6.0)

    cfg = SgDeskConfig.load()
    if args.port:
        cfg.port = args.port
    if args.server:
        cfg.relay_url = ""
        cfg.save()
    elif args.relay is not None:
        cfg.relay_url = args.relay.strip().rstrip("/")
        cfg.save()

    local_url = f"http://localhost:{cfg.port}"

    # If already running, just open the UI window and exit
    if _is_port_open(cfg.port):
        if not args.no_browser:
            open_app_window(local_url, delay=0.0)
        return

    print("=" * 54)
    print("  sgDesk Remote Desktop")
    print(f"  Desk ID      : {cfg.desk_id}")
    print(f"  Session PIN  : {cfg.session_pin}")
    print(f"  Local URL    : {local_url}")
    print(f"  Relay URL    : {cfg.relay_url or '(Relay Server Mode)'}")
    print("=" * 54)

    server_holder: dict[str, uvicorn.Server | None] = {"server": None}

    def request_shutdown() -> None:
        srv = server_holder.get("server")
        if srv is not None:
            srv.should_exit = True

    tray: SystemTrayIcon | None = None
    if not args.no_tray and not args.no_browser:
        def _tray_elevate() -> None:
            if request_self_elevation(sys.argv[1:]):
                request_shutdown()

        tray = SystemTrayIcon(
            desk_id=cfg.desk_id,
            is_admin=is_admin(),
            on_open=lambda: threading.Thread(
                target=open_app_window, args=(local_url, 0.0), daemon=True
            ).start(),
            on_copy_id=lambda: pyperclip.copy(cfg.desk_id),
            on_elevate=_tray_elevate,
            on_quit=request_shutdown,
        )
        tray.start()

    if not args.no_browser:
        threading.Thread(target=open_app_window, args=(local_url, 1.0), daemon=True).start()

    app = create_app(cfg, on_shutdown_request=request_shutdown)
    uv_config = uvicorn.Config(app, host=cfg.host, port=cfg.port, log_config=None)
    server = uvicorn.Server(uv_config)
    server_holder["server"] = server
    try:
        server.run()
    finally:
        if tray is not None:
            tray.stop()


if __name__ == "__main__":
    main()
