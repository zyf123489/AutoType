"""Send Unicode text as Windows keyboard input without using the clipboard."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("data", INPUT_UNION)]


_send_input = ctypes.WinDLL("user32", use_last_error=True).SendInput
_send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_send_input.restype = wintypes.UINT


def send_unicode_char(char: str) -> None:
    """Type one Unicode scalar using VK_PACKET key-down/up events."""
    if len(char) != 1:
        raise ValueError("Expected a single Unicode character")

    encoded = char.encode("utf-16-le")
    units = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]
    events = (INPUT * (2 * len(units)))()
    for index, unit in enumerate(units):
        events[2 * index] = INPUT(
            INPUT_KEYBOARD, INPUT_UNION(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0))
        )
        events[2 * index + 1] = INPUT(
            INPUT_KEYBOARD,
            INPUT_UNION(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)),
        )

    sent = _send_input(len(events), events, ctypes.sizeof(INPUT))
    if sent != len(events):
        error = ctypes.get_last_error()
        raise OSError(error, f"Unicode input failed: sent {sent}/{len(events)} events")
