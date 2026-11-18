"""Configuration management for sgDesk."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import socket
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    _appdata = Path(os.environ.get("APPDATA") or Path.home())
    CONFIG_DIR = _appdata / "sgDesk"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE = CONFIG_DIR / "sgdesk_config.json"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    CONFIG_DIR = BASE_DIR
    CONFIG_FILE = BASE_DIR / "sgdesk_config.json"
DEFAULT_RELAY_URL = "https://remote.step.my.id"


def get_machine_fingerprint() -> str:
    """Return a stable per-PC fingerprint so copied configs never share a Desk ID."""
    raw = f"{socket.gethostname()}-{uuid.getnode()}"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            if guid:
                raw = str(guid)
    except Exception:
        pass
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def normalize_desk_id(desk_id: str) -> str:
    """Normalize a Desk ID by stripping spaces and dashes and uppercasing."""
    return re.sub(r"[\s-]+", "", desk_id or "").upper()


def generate_desk_id() -> str:
    """Generate an AnyDesk-style 9-digit Desk ID like '482 910 374'."""
    a = random.randint(100, 999)
    b = random.randint(100, 999)
    c = random.randint(100, 999)
    return f"{a} {b} {c}"


def generate_pin() -> str:
    """Generate a 6-digit numeric session PIN."""
    return f"{random.randint(100000, 999999)}"


@dataclass
class SgDeskConfig:
    """Persistent and runtime configuration for sgDesk."""

    desk_id: str = field(default_factory=generate_desk_id)
    hostname: str = field(default_factory=socket.gethostname)
    machine_id: str = field(default_factory=get_machine_fingerprint)
    port: int = 3737
    host: str = "0.0.0.0"
    unattended_password: str = ""
    require_approval: bool = True
    allow_input: bool = True
    default_quality: int = 75
    default_fps: int = 24
    default_scale: float = 1.0
    tunnel_url: str = ""
    relay_url: str = DEFAULT_RELAY_URL
    auto_quick_tunnel: bool = False

    # Runtime-only fields (not persisted to disk)
    session_pin: str = field(default_factory=generate_pin, repr=False)
    host_admin_token: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)

    @classmethod
    def load(cls) -> "SgDeskConfig":
        """Load configuration from disk or create defaults."""
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                valid_fields = {
                    "desk_id",
                    "hostname",
                    "machine_id",
                    "port",
                    "host",
                    "unattended_password",
                    "require_approval",
                    "allow_input",
                    "default_quality",
                    "default_fps",
                    "default_scale",
                    "tunnel_url",
                    "relay_url",
                    "auto_quick_tunnel",
                }
                filtered = {k: v for k, v in data.items() if k in valid_fields}
                cfg = cls(**filtered)
                current_fp = get_machine_fingerprint()
                if cfg.machine_id != current_fp:
                    cfg.machine_id = current_fp
                    cfg.desk_id = generate_desk_id()
                    cfg.hostname = socket.gethostname()
                    cfg.save()
                elif not cfg.hostname:
                    cfg.hostname = socket.gethostname()
                return cfg
            except Exception:
                pass
        cfg = cls()
        cfg.save()
        return cfg

    def save(self) -> None:
        """Persist non-ephemeral settings to sgdesk_config.json."""
        data = asdict(self)
        data.pop("session_pin", None)
        data.pop("host_admin_token", None)
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def regenerate_pin(self) -> str:
        """Generate and return a new 6-digit session PIN."""
        self.session_pin = generate_pin()
        return self.session_pin

    def verify_secret(self, secret: str) -> bool:
        """Check whether the provided PIN or unattended password is valid."""
        if not secret:
            return False
        if secrets.compare_digest(secret, self.session_pin):
            return True
        if self.unattended_password and secrets.compare_digest(secret, self.unattended_password):
            return True
        return False
