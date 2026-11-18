"""High-speed Windows screen capture and JPEG encoding for sgDesk."""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from typing import Optional

import mss
from PIL import Image


@dataclass
class MonitorInfo:
    index: int
    name: str
    left: int
    top: int
    width: int
    height: int
    is_primary: bool


class ScreenCapturer:
    """Thread-safe screen capturer using mss (DXGI/GDI)."""

    def __init__(self) -> None:
        self._local = threading.local()

    def _get_sct(self) -> mss.mss:
        sct = getattr(self._local, "sct", None)
        if sct is None:
            sct = mss.mss()
            self._local.sct = sct
        return sct

    def get_monitors(self) -> list[MonitorInfo]:
        """Return available monitors (index 0 = All Monitors, 1+ = individual)."""
        sct = self._get_sct()
        result: list[MonitorInfo] = []
        for idx, mon in enumerate(sct.monitors):
            if idx == 0:
                name = f"All Displays ({mon['width']}x{mon['height']})"
            else:
                name = f"Display {idx} ({mon['width']}x{mon['height']})"
            result.append(
                MonitorInfo(
                    index=idx,
                    name=name,
                    left=mon["left"],
                    top=mon["top"],
                    width=mon["width"],
                    height=mon["height"],
                    is_primary=(idx == 1),
                )
            )
        return result

    def get_monitor_rect(self, monitor_idx: int) -> dict:
        """Return mss monitor rect dict, clamped to valid indices."""
        sct = self._get_sct()
        monitors = sct.monitors
        if 0 <= monitor_idx < len(monitors):
            return monitors[monitor_idx]
        return monitors[1] if len(monitors) > 1 else monitors[0]

    def capture_jpeg(
        self,
        monitor_idx: int = 1,
        quality: int = 75,
        scale: float = 1.0,
        prev_raw: Optional[bytes] = None,
    ) -> tuple[Optional[bytes], int, int, dict, Optional[bytes]]:
        """
        Capture the specified monitor and encode as JPEG.

        Returns:
            (jpeg_bytes_or_None_if_unchanged, out_width, out_height, monitor_rect, raw_bgra_bytes)
        """
        sct = self._get_sct()
        monitors = sct.monitors
        if not (0 <= monitor_idx < len(monitors)):
            monitor_idx = 1 if len(monitors) > 1 else 0
        mon = monitors[monitor_idx]

        sct_img = sct.grab(mon)
        raw = bytes(sct_img.bgra)

        if prev_raw is not None and raw == prev_raw:
            return None, sct_img.width, sct_img.height, mon, prev_raw

        img = Image.frombytes("RGB", sct_img.size, raw, "raw", "BGRX")

        scale = max(0.25, min(1.0, float(scale)))
        if scale < 0.99:
            new_w = max(1, int(img.width * scale))
            new_h = max(1, int(img.height * scale))
            img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)

        out_w, out_h = img.size
        buf = io.BytesIO()
        q = max(10, min(95, int(quality)))
        img.save(buf, format="JPEG", quality=q, optimize=False)
        return buf.getvalue(), out_w, out_h, mon, raw
