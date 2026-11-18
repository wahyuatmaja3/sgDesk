"""Transparent click-through Win32 overlay rendering per-user virtual cursors."""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

LWA_COLORKEY = 0x00000001
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_ERASEBKGND = 0x0014

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

TRANSPARENT_BK = 1
DT_LEFT = 0x00000000
DT_SINGLELINE = 0x00000020
DT_VCENTER = 0x00000004
DT_CALCRECT = 0x00000400

# Distinct BGR colors (avoid pure black 0x000000 which is our colorkey)
CURSOR_PALETTE_BGR = [
    0x4444EF,  # Red
    0xF6823B,  # Blue
    0x81B910,  # Emerald
    0x0B9EF5,  # Amber
    0xF65C8B,  # Violet
    0x9948EC,  # Pink
    0xD4B606,  # Cyan
]

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


@dataclass
class _RemoteCursor:
    session_id: str
    label: str
    x: int
    y: int
    color_bgr: int
    updated_at: float


class RemoteCursorOverlay:
    """Renders per-session colored cursors on a transparent click-through Win32 overlay."""

    def __init__(self, ttl_seconds: float = 12.0) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cursors: dict[str, _RemoteCursor] = {}
        self._color_map: dict[str, int] = {}
        self._next_color = 0
        self._dirty = False
        self._hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._visible = False
        self._wndproc_ref = None

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="sgDesk-CursorOverlay", daemon=True)
        self._thread.start()

    def update_cursor(self, session_id: str, label: str, x: int, y: int) -> None:
        """Update or create a virtual cursor for a remote session."""
        self._ensure_started()
        with self._lock:
            if session_id not in self._color_map:
                self._color_map[session_id] = CURSOR_PALETTE_BGR[
                    self._next_color % len(CURSOR_PALETTE_BGR)
                ]
                self._next_color += 1
            clean_label = (label or "Remote").replace(" (Relay)", "").strip()[:20]
            self._cursors[session_id] = _RemoteCursor(
                session_id=session_id,
                label=clean_label,
                x=int(x),
                y=int(y),
                color_bgr=self._color_map[session_id],
                updated_at=time.monotonic(),
            )
            self._dirty = True

    def remove_cursor(self, session_id: str) -> None:
        """Remove a remote session's virtual cursor."""
        with self._lock:
            if session_id in self._cursors:
                self._cursors.pop(session_id, None)
                self._dirty = True

    def stop(self) -> None:
        """Shut down the overlay window and thread."""
        self._stop_event.set()
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_DESTROY, 0, 0)
            except Exception:
                pass

    def _get_virtual_rect(self) -> tuple[int, int, int, int]:
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        return vx, vy, vw, vh

    def _draw_cursors(self, hdc: int, vx: int, vy: int, vw: int, vh: int) -> None:
        mem_dc = gdi32.CreateCompatibleDC(hdc)
        mem_bmp = gdi32.CreateCompatibleBitmap(hdc, vw, vh)
        old_bmp = gdi32.SelectObject(mem_dc, mem_bmp)

        # Fill background with pure black (transparent colorkey)
        bg_brush = gdi32.CreateSolidBrush(0x000000)
        full_rect = wintypes.RECT(0, 0, vw, vh)
        user32.FillRect(mem_dc, ctypes.byref(full_rect), bg_brush)
        gdi32.DeleteObject(bg_brush)

        font = gdi32.CreateFontW(
            15, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 0, 0, "Segoe UI"
        )
        old_font = gdi32.SelectObject(mem_dc, font)
        gdi32.SetBkMode(mem_dc, TRANSPARENT_BK)

        with self._lock:
            cursors = list(self._cursors.values())

        border_pen = gdi32.CreatePen(0, 2, 0x010101)
        for cur in cursors:
            cx = cur.x - vx
            cy = cur.y - vy

            # Crisp arrow polygon relative to (cx, cy)
            pts = (wintypes.POINT * 7)(
                wintypes.POINT(cx, cy),
                wintypes.POINT(cx, cy + 20),
                wintypes.POINT(cx + 5, cy + 15),
                wintypes.POINT(cx + 9, cy + 23),
                wintypes.POINT(cx + 12, cy + 21),
                wintypes.POINT(cx + 8, cy + 13),
                wintypes.POINT(cx + 15, cy + 13),
            )
            fill_brush = gdi32.CreateSolidBrush(cur.color_bgr)
            old_pen = gdi32.SelectObject(mem_dc, border_pen)
            old_brush = gdi32.SelectObject(mem_dc, fill_brush)
            gdi32.Polygon(mem_dc, pts, 7)
            gdi32.SelectObject(mem_dc, old_brush)
            gdi32.SelectObject(mem_dc, old_pen)

            # Label pill next to the arrow
            calc_rc = wintypes.RECT(0, 0, 0, 0)
            user32.DrawTextW(mem_dc, cur.label, -1, ctypes.byref(calc_rc), DT_CALCRECT | DT_SINGLELINE)
            pad_x, pad_y = 7, 3
            pill_x = cx + 16
            pill_y = cy + 16
            pill_w = (calc_rc.right - calc_rc.left) + pad_x * 2
            pill_h = (calc_rc.bottom - calc_rc.top) + pad_y * 2

            pill_pen = gdi32.CreatePen(0, 1, 0x010101)
            old_pen = gdi32.SelectObject(mem_dc, pill_pen)
            old_brush = gdi32.SelectObject(mem_dc, fill_brush)
            gdi32.RoundRect(mem_dc, pill_x, pill_y, pill_x + pill_w, pill_y + pill_h, 8, 8)
            gdi32.SelectObject(mem_dc, old_brush)
            gdi32.SelectObject(mem_dc, old_pen)
            gdi32.DeleteObject(pill_pen)
            gdi32.DeleteObject(fill_brush)

            gdi32.SetTextColor(mem_dc, 0xFFFFFF)
            text_rc = wintypes.RECT(pill_x + pad_x, pill_y + pad_y, pill_x + pill_w - pad_x, pill_y + pill_h - pad_y)
            user32.DrawTextW(mem_dc, cur.label, -1, ctypes.byref(text_rc), DT_LEFT | DT_SINGLELINE | DT_VCENTER)

        gdi32.DeleteObject(border_pen)
        gdi32.SelectObject(mem_dc, old_font)
        gdi32.DeleteObject(font)

        gdi32.BitBlt(hdc, 0, 0, vw, vh, mem_dc, 0, 0, 0x00CC0020)  # SRCCOPY
        gdi32.SelectObject(mem_dc, old_bmp)
        gdi32.DeleteObject(mem_bmp)
        gdi32.DeleteDC(mem_dc)

    def _run_loop(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        class_name = "sgDeskCursorOverlayWnd"

        def _wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
                vx, vy, vw, vh = self._get_virtual_rect()
                self._draw_cursors(hdc, vx, vy, vw, vh)
                user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == WM_TIMER:
                now = time.monotonic()
                with self._lock:
                    expired = [k for k, v in self._cursors.items() if now - v.updated_at > self._ttl]
                    for k in expired:
                        self._cursors.pop(k, None)
                        self._dirty = True
                    has_cursors = bool(self._cursors)
                    dirty = self._dirty
                    self._dirty = False

                if has_cursors and not self._visible:
                    vx, vy, vw, vh = self._get_virtual_rect()
                    user32.SetWindowPos(
                        hwnd, HWND_TOPMOST, vx, vy, vw, vh, SWP_NOACTIVATE
                    )
                    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                    self._visible = True
                elif not has_cursors and self._visible:
                    user32.ShowWindow(hwnd, SW_HIDE)
                    self._visible = False

                if dirty and self._visible:
                    user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(_wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        user32.RegisterClassExW(ctypes.byref(wc))

        vx, vy, vw, vh = self._get_virtual_rect()
        ex_style = (
            WS_EX_LAYERED
            | WS_EX_TRANSPARENT
            | WS_EX_TOPMOST
            | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE
        )
        hwnd = user32.CreateWindowExW(
            ex_style,
            class_name,
            "sgDesk Virtual Cursors",
            WS_POPUP,
            vx,
            vy,
            vw,
            vh,
            None,
            None,
            hinst,
            None,
        )
        if not hwnd:
            return

        self._hwnd = hwnd
        user32.SetLayeredWindowAttributes(hwnd, 0x000000, 0, LWA_COLORKEY)
        user32.SetTimer(hwnd, 1, 33, None)

        msg = wintypes.MSG()
        while not self._stop_event.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._hwnd = None
