"""Cloudflare Tunnel (cloudflared) detection and Quick Tunnel manager."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import psutil

from app.config import BASE_DIR

TRY_CF_REGEX = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
CLOUDFLARED_DOWNLOAD_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
)


@dataclass
class TunnelStatus:
    cloudflared_installed: bool
    cloudflared_version: str
    system_worker_running: bool
    quick_tunnel_running: bool
    quick_tunnel_url: str
    quick_tunnel_error: str
    downloading: bool = False


class CloudflareTunnelManager:
    """Detects system/bundled cloudflared worker and manages Quick Tunnel."""

    def __init__(self, on_update: Optional[Callable[[], None]] = None) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._quick_url: str = ""
        self._error: str = ""
        self._downloading: bool = False
        self._lock = threading.Lock()
        self._version_cache: Optional[str] = None
        self._on_update = on_update

    def _notify_update(self) -> None:
        if self._on_update:
            try:
                self._on_update()
            except Exception:
                pass

    def _get_binary(self) -> Optional[str]:
        candidates = [
            BASE_DIR / "cloudflared.exe",
            BASE_DIR / "bin" / "cloudflared.exe",
        ]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.insert(1, Path(meipass) / "cloudflared.exe")
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)

        found = shutil.which("cloudflared")
        if found:
            return found

        for sys_path in (
            Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
            Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
        ):
            if sys_path.is_file():
                return str(sys_path)
        return None

    def _get_version(self) -> str:
        if self._version_cache is not None:
            return self._version_cache
        binary = self._get_binary()
        if not binary:
            self._version_cache = ""
            return ""
        try:
            out = subprocess.check_output(
                [binary, "--version"], stderr=subprocess.STDOUT, text=True, timeout=3
            ).strip()
            self._version_cache = out.splitlines()[0] if out else "installed"
        except Exception:
            self._version_cache = "installed"
        return self._version_cache

    def _is_system_worker_running(self) -> bool:
        """Check if any cloudflared process is running outside our own quick-tunnel child."""
        own_pid = self._proc.pid if self._proc and self._proc.poll() is None else None
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "cloudflared" in name and proc.info.get("pid") != own_pid:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def status(self) -> TunnelStatus:
        binary = self._get_binary()
        quick_running = self._proc is not None and self._proc.poll() is None
        return TunnelStatus(
            cloudflared_installed=bool(binary),
            cloudflared_version=self._get_version(),
            system_worker_running=self._is_system_worker_running(),
            quick_tunnel_running=quick_running,
            quick_tunnel_url=self._quick_url if quick_running else "",
            quick_tunnel_error=self._error,
            downloading=self._downloading,
        )

    def _spawn_quick_tunnel(self, binary: str, port: int) -> None:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creation_flags,
        )
        threading.Thread(target=self._read_output, args=(self._proc,), daemon=True).start()

    def _download_and_start(self, port: int) -> None:
        dest = BASE_DIR / "cloudflared.exe"
        tmp = BASE_DIR / "cloudflared.exe.part"
        try:
            urllib.request.urlretrieve(CLOUDFLARED_DOWNLOAD_URL, str(tmp))
            tmp.replace(dest)
            self._version_cache = None
        except Exception as exc:
            with self._lock:
                self._downloading = False
                self._error = f"Failed to download cloudflared: {exc}"
            self._notify_update()
            return

        with self._lock:
            self._downloading = False
            if self._proc is None or self._proc.poll() is not None:
                self._spawn_quick_tunnel(str(dest), port)
        self._notify_update()

    def start_quick_tunnel(self, port: int) -> TunnelStatus:
        """Start `cloudflared tunnel --url http://localhost:<port>` in background."""
        with self._lock:
            if (self._proc is not None and self._proc.poll() is None) or self._downloading:
                return self.status()
            self._quick_url = ""
            self._error = ""
            binary = self._get_binary()
            if not binary:
                self._downloading = True
                threading.Thread(
                    target=self._download_and_start, args=(port,), daemon=True
                ).start()
                return self.status()

            self._spawn_quick_tunnel(binary, port)
            return self.status()

    def _read_output(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            m = TRY_CF_REGEX.search(line)
            if m:
                self._quick_url = m.group(0)
                self._notify_update()
            if "failed" in line.lower() and not self._quick_url:
                self._error = line.strip()[:200]
                self._notify_update()
        if proc.returncode and not self._quick_url and not self._error:
            self._error = f"cloudflared exited with code {proc.returncode}"
            self._notify_update()

    def stop_quick_tunnel(self) -> TunnelStatus:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
            self._quick_url = ""
            self._error = ""
            return self.status()
