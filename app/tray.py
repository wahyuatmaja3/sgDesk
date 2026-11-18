"""Lightweight Win32 System Tray icon for sgDesk (no external dependencies)."""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Optional

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001
TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020
TPM_RETURNCMD = 0x0100

IDI_APPLICATION = 32512

ID_OPEN = 1001
ID_COPY_ID = 1002
ID_ELEVATE = 1003
ID_QUIT = 1004

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.CreateWindowExW.restype = wintypes.HWND
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


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


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class SystemTrayIcon:
    """Manages the sgDesk icon and context menu in the Windows System Tray."""

    def __init__(
        self,
        desk_id: str,
        is_admin: bool,
        on_open: Callable[[], None],
        on_copy_id: Callable[[], None],
        on_elevate: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self.desk_id = desk_id
        self.is_admin = is_admin
        self.on_open = on_open
        self.on_copy_id = on_copy_id
        self.on_elevate = on_elevate
        self.on_quit = on_quit

        self._hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wndproc_ref = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="sgDesk-Tray", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_DESTROY, 0, 0)
            except Exception:
                pass

    def _show_menu(self, hwnd: int) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open sgDesk")
        user32.AppendMenuW(menu, MF_STRING, ID_COPY_ID, f"Copy ID ({self.desk_id})")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        if self.is_admin:
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, ID_ELEVATE, "Running as Administrator")
        else:
            user32.AppendMenuW(menu, MF_STRING, ID_ELEVATE, "Run as Administrator")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit sgDesk")

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(
            menu,
            TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD,
            pt.x,
            pt.y,
            0,
            hwnd,
            None,
        )
        user32.DestroyMenu(menu)

        if cmd == ID_OPEN:
            self.on_open()
        elif cmd == ID_COPY_ID:
            self.on_copy_id()
        elif cmd == ID_ELEVATE and not self.is_admin:
            self.on_elevate()
        elif cmd == ID_QUIT:
            self.on_quit()

    def _run_loop(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        class_name = "sgDeskTrayWnd"

        def _wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_TRAYICON:
                ev = lparam & 0xFFFF
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.on_open()
                elif ev == WM_RBUTTONUP:
                    self._show_menu(hwnd)
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

        hwnd = user32.CreateWindowExW(
            0, class_name, "sgDesk Tray", 0, 0, 0, 0, 0, None, None, hinst, None
        )
        if not hwnd:
            return
        self._hwnd = hwnd

        hicon = user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = hicon
        tip = f"sgDesk ({self.desk_id})" + (" [Admin]" if self.is_admin else "")
        nid.szTip = tip[:127]

        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        msg = wintypes.MSG()
        while not self._stop_event.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self._hwnd = None
