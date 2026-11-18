"""Native Windows mouse & keyboard injection via ctypes SendInput."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


EXTENDED_VKS = {
    0x21,  # PageUp
    0x22,  # PageDown
    0x23,  # End
    0x24,  # Home
    0x25,  # Left
    0x26,  # Up
    0x27,  # Right
    0x28,  # Down
    0x2C,  # PrintScreen
    0x2D,  # Insert
    0x2E,  # Delete
    0x5B,  # LWin
    0x5C,  # RWin
    0x5D,  # Apps
    0xA3,  # RControl
    0xA5,  # RAlt
    0x6F,  # NumpadDivide
}

# Mapping from browser KeyboardEvent.code to Windows Virtual-Key (VK)
CODE_TO_VK: dict[str, int] = {
    # Letters
    **{f"Key{chr(c)}": c for c in range(ord("A"), ord("Z") + 1)},
    # Top-row digits
    **{f"Digit{d}": ord("0") + d for d in range(10)},
    # Numpad digits
    **{f"Numpad{d}": 0x60 + d for d in range(10)},
    # Function keys
    **{f"F{i}": 0x6F + i for i in range(1, 25)},
    # Control / Navigation / Editing
    "Backspace": 0x08,
    "Tab": 0x09,
    "Enter": 0x0D,
    "NumpadEnter": 0x0D,
    "ShiftLeft": 0xA0,
    "ShiftRight": 0xA1,
    "ControlLeft": 0xA2,
    "ControlRight": 0xA3,
    "AltLeft": 0xA4,
    "AltRight": 0xA5,
    "Pause": 0x13,
    "CapsLock": 0x14,
    "Escape": 0x1B,
    "Space": 0x20,
    "PageUp": 0x21,
    "PageDown": 0x22,
    "End": 0x23,
    "Home": 0x24,
    "ArrowLeft": 0x25,
    "ArrowUp": 0x26,
    "ArrowRight": 0x27,
    "ArrowDown": 0x28,
    "PrintScreen": 0x2C,
    "Insert": 0x2D,
    "Delete": 0x2E,
    "MetaLeft": 0x5B,
    "MetaRight": 0x5C,
    "ContextMenu": 0x5D,
    "NumLock": 0x90,
    "ScrollLock": 0x91,
    # Numpad operators
    "NumpadMultiply": 0x6A,
    "NumpadAdd": 0x6B,
    "NumpadSubtract": 0x6D,
    "NumpadDecimal": 0x6E,
    "NumpadDivide": 0x6F,
    # Punctuation
    "Semicolon": 0xBA,
    "Equal": 0xBB,
    "Comma": 0xBC,
    "Minus": 0xBD,
    "Period": 0xBE,
    "Slash": 0xBF,
    "Backquote": 0xC0,
    "BracketLeft": 0xDB,
    "Backslash": 0xDC,
    "BracketRight": 0xDD,
    "Quote": 0xDE,
}

ALIAS_TO_VK: dict[str, int] = {
    "ctrl": 0xA2,
    "control": 0xA2,
    "shift": 0xA0,
    "alt": 0xA4,
    "win": 0x5B,
    "meta": 0x5B,
    "esc": 0x1B,
    "tab": 0x09,
    "enter": 0x0D,
    "del": 0x2E,
    "delete": 0x2E,
    "prtsc": 0x2C,
    "d": 0x44,
    "l": 0x4C,
    "r": 0x52,
    "e": 0x45,
}


def _send_input(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def norm_to_screen_px(norm_x: float, norm_y: float, monitor_rect: dict) -> tuple[int, int]:
    """Convert normalized [0..1] coordinates on monitor_rect to Windows screen pixels."""
    nx = max(0.0, min(1.0, float(norm_x)))
    ny = max(0.0, min(1.0, float(norm_y)))

    px = int(round(monitor_rect["left"] + nx * max(1, monitor_rect["width"] - 1)))
    py = int(round(monitor_rect["top"] + ny * max(1, monitor_rect["height"] - 1)))
    return px, py


def _norm_to_virtual_abs(norm_x: float, norm_y: float, monitor_rect: dict) -> tuple[int, int]:
    """Convert normalized [0..1] coordinates on monitor_rect to SendInput 0..65535 virtual desktop coords."""
    px, py = norm_to_screen_px(norm_x, norm_y, monitor_rect)

    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))

    abs_x = int(((px - vx) * 65535.0) / max(1, vw - 1))
    abs_y = int(((py - vy) * 65535.0) / max(1, vh - 1))
    return max(0, min(65535, abs_x)), max(0, min(65535, abs_y))


def move_mouse(norm_x: float, norm_y: float, monitor_rect: dict) -> None:
    abs_x, abs_y = _norm_to_virtual_abs(norm_x, norm_y, monitor_rect)
    inp = INPUT(
        type=INPUT_MOUSE,
        union=_INPUT_UNION(
            mi=MOUSEINPUT(
                dx=abs_x,
                dy=abs_y,
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    _send_input(inp)


def mouse_button(button: str, action: str, norm_x: float, norm_y: float, monitor_rect: dict) -> None:
    abs_x, abs_y = _norm_to_virtual_abs(norm_x, norm_y, monitor_rect)
    btn_map = {
        ("left", "down"): MOUSEEVENTF_LEFTDOWN,
        ("left", "up"): MOUSEEVENTF_LEFTUP,
        ("right", "down"): MOUSEEVENTF_RIGHTDOWN,
        ("right", "up"): MOUSEEVENTF_RIGHTUP,
        ("middle", "down"): MOUSEEVENTF_MIDDLEDOWN,
        ("middle", "up"): MOUSEEVENTF_MIDDLEUP,
    }
    flag = btn_map.get((button, action))
    if flag is None:
        return
    inp = INPUT(
        type=INPUT_MOUSE,
        union=_INPUT_UNION(
            mi=MOUSEINPUT(
                dx=abs_x,
                dy=abs_y,
                mouseData=0,
                dwFlags=flag | MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    _send_input(inp)


def mouse_wheel(delta_x: float, delta_y: float) -> None:
    events: list[INPUT] = []
    if delta_y:
        # Browser wheel deltaY > 0 means scroll down, Windows WHEEL_DELTA < 0 means scroll down
        wheel_amt = int(-delta_y)
        if wheel_amt == 0:
            wheel_amt = -120 if delta_y > 0 else 120
        events.append(
            INPUT(
                type=INPUT_MOUSE,
                union=_INPUT_UNION(
                    mi=MOUSEINPUT(
                        dx=0,
                        dy=0,
                        mouseData=ctypes.c_ulong(wheel_amt).value,
                        dwFlags=MOUSEEVENTF_WHEEL,
                        time=0,
                        dwExtraInfo=None,
                    )
                ),
            )
        )
    if delta_x:
        hwheel_amt = int(delta_x)
        events.append(
            INPUT(
                type=INPUT_MOUSE,
                union=_INPUT_UNION(
                    mi=MOUSEINPUT(
                        dx=0,
                        dy=0,
                        mouseData=ctypes.c_ulong(hwheel_amt).value,
                        dwFlags=MOUSEEVENTF_HWHEEL,
                        time=0,
                        dwExtraInfo=None,
                    )
                ),
            )
        )
    if events:
        _send_input(*events)


def key_event(code: str, action: str) -> None:
    vk = CODE_TO_VK.get(code)
    if vk is None:
        return
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    if action == "up":
        flags |= KEYEVENTF_KEYUP
    inp = INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUT_UNION(
            ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
        ),
    )
    _send_input(inp)


def send_hotkey(keys: list[str]) -> None:
    """Press and release a combination of keys (e.g. ['ctrl', 'shift', 'esc'])."""
    vks: list[int] = []
    for k in keys:
        vk = CODE_TO_VK.get(k) or ALIAS_TO_VK.get(k.lower())
        if vk is not None:
            vks.append(vk)
    if not vks:
        return

    inputs: list[INPUT] = []
    for vk in vks:
        scan = user32.MapVirtualKeyW(vk, 0)
        flags = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0
        inputs.append(
            INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(
                    ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
                ),
            )
        )
    for vk in reversed(vks):
        scan = user32.MapVirtualKeyW(vk, 0)
        flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0)
        inputs.append(
            INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(
                    ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
                ),
            )
        )
    _send_input(*inputs)
