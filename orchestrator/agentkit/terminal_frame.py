"""Windows window-only capture. Never samples the desktop or sends input."""
from __future__ import annotations

import ctypes
import os
import struct
import zlib
from ctypes import wintypes as w

MAX_PIXELS = 4_194_304


def user32():
    if os.name != "nt":
        raise OSError("Actual window mirroring currently requires Windows.")
    user = ctypes.WinDLL("user32", use_last_error=True)
    for name, args, result in (
        ("IsWindow", [w.HWND], w.BOOL), ("IsWindowVisible", [w.HWND], w.BOOL),
        ("IsIconic", [w.HWND], w.BOOL),
        ("GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
        ("GetWindowTextW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        ("GetClientRect", [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
        ("GetDC", [w.HWND], w.HDC), ("ReleaseDC", [w.HWND, w.HDC], ctypes.c_int),
        ("PrintWindow", [w.HWND, w.HDC, w.UINT], w.BOOL),
    ):
        function = getattr(user, name)
        function.argtypes, function.restype = args, result
    return user


def title(hwnd: int) -> str:
    text = ctypes.create_unicode_buffer(512)
    user32().GetWindowTextW(hwnd, text, len(text))
    return text.value


def owner(hwnd: int) -> int:
    pid = w.DWORD()
    user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def console_window(prefix: str) -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleWindow.restype = w.HWND
    user = user32()
    hwnd = kernel.GetConsoleWindow()
    if hwnd and user.IsWindowVisible(hwnd) and title(hwnd).startswith(prefix):
        return int(hwnd)
    # Windows Terminal's pseudoconsole handle itself is not a visible window.
    # Only its exact unique active-tab title can bind a real hosting window.
    matches = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    @callback_type
    def callback(candidate, _):
        if user.IsWindowVisible(candidate) and title(candidate).startswith(prefix):
            matches.append(int(candidate))
        return True
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumWindows(callback, 0)
    return matches[0] if len(matches) == 1 else 0


def png(width: int, height: int, bgra: bytes) -> bytes:
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS or len(bgra) != width * height * 4:
        raise ValueError("Invalid terminal image size.")
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = width * 3
    pixels = b"".join(b"\0" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(pixels, 3)) + chunk(b"IEND", b""))


def capture(hwnd: int) -> bytes:
    user = user32()
    if not user.IsWindow(hwnd) or not user.IsWindowVisible(hwnd):
        raise OSError("The registered CLI window is closed or not visible.")
    if user.IsIconic(hwnd):
        raise OSError("CLI window is minimized; restore it to refresh the mirror.")
    rectangle = w.RECT()
    if not user.GetClientRect(hwnd, ctypes.byref(rectangle)):
        raise OSError("CLI window dimensions unavailable.")
    width, height = rectangle.right, rectangle.bottom
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise OSError("CLI window dimensions exceed the capture limit.")
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    for name, args, result in (
        ("CreateCompatibleDC", [w.HDC], w.HDC),
        ("CreateCompatibleBitmap", [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP),
        ("SelectObject", [w.HDC, w.HANDLE], w.HANDLE),
        ("DeleteObject", [w.HANDLE], w.BOOL), ("DeleteDC", [w.HDC], w.BOOL),
        ("GetDIBits", [w.HDC, w.HBITMAP, w.UINT, w.UINT, w.LPVOID, w.LPVOID, w.UINT], ctypes.c_int),
    ):
        function = getattr(gdi, name)
        function.argtypes, function.restype = args, result
    window_dc = user.GetDC(hwnd)
    memory_dc = gdi.CreateCompatibleDC(window_dc)
    bitmap = gdi.CreateCompatibleBitmap(window_dc, width, height)
    previous = gdi.SelectObject(memory_dc, bitmap)
    try:
        if not window_dc or not memory_dc or not bitmap or not user.PrintWindow(hwnd, memory_dc, 1):
            raise OSError("This terminal host cannot provide a window frame.")
        gdi.SelectObject(memory_dc, previous)
        header = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0))
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi.GetDIBits(memory_dc, bitmap, 0, height, pixels, header, 0) != height:
            raise OSError("Terminal pixels unavailable.")
        return png(width, height, pixels.raw)
    finally:
        gdi.SelectObject(memory_dc, previous)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory_dc)
        user.ReleaseDC(hwnd, window_dc)
