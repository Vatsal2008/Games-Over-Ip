"""Save a Tk window to PNG (Windows only).

    from tools.snapshot import capture
    capture(root, "shots/menu.png")          # client area of root's toplevel

Uses user32.PrintWindow(PW_RENDERFULLCONTENT) into a 32-bit DIB section, converts
BGRA -> RGBA and encodes the PNG with goip.gfx, so no third-party packages are needed.
The window must be mapped (not minimised); it may be covered by other windows.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional, Tuple

PW_RENDERFULLCONTENT = 2

if __package__ in (None, ""):  # allow "python tools/snapshot.py"
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from goip.gfx import encode_png  # noqa: E402


def _api():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    H = ctypes.c_void_p
    user32.GetDC.restype = H
    user32.GetDC.argtypes = [H]
    user32.ReleaseDC.argtypes = [H, H]
    user32.GetWindowRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [H, ctypes.POINTER(wintypes.POINT)]
    user32.PrintWindow.argtypes = [H, H, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.restype = H
    gdi32.CreateCompatibleDC.argtypes = [H]
    gdi32.CreateDIBSection.restype = H
    gdi32.CreateDIBSection.argtypes = [H, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       H, wintypes.DWORD]
    gdi32.SelectObject.restype = H
    gdi32.SelectObject.argtypes = [H, H]
    gdi32.DeleteObject.argtypes = [H]
    gdi32.DeleteDC.argtypes = [H]
    return ctypes, wintypes, user32, gdi32


def grab(widget: Any, client_only: bool = True) -> Tuple[int, int, bytearray]:
    """Return (w, h, rgba) of the widget's toplevel window."""
    if sys.platform != "win32":
        raise RuntimeError("tools.snapshot only works on Windows (it uses user32.PrintWindow)")
    ctypes, wintypes, user32, gdi32 = _api()
    top = widget.winfo_toplevel()
    if not top.winfo_viewable():
        top.update()            # map the window and let it draw once
    # Flush pending geometry and redraws. While animations run, a full update() would
    # keep servicing their timers until they finish, so the shot would not show them.
    top.update_idletasks()
    hwnd = int(top.wm_frame(), 16)
    wr = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
        raise RuntimeError("GetWindowRect failed (is the window mapped?)")
    w, h = wr.right - wr.left, wr.bottom - wr.top
    if w <= 0 or h <= 0:
        raise RuntimeError("window has no size")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount, bmi.biCompression = w, -h, 1, 32, 0
    bmi_buf = ctypes.create_string_buffer(ctypes.sizeof(bmi) + 16)
    ctypes.memmove(bmi_buf, ctypes.byref(bmi), ctypes.sizeof(bmi))

    screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(screen)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc, bmi_buf, 0, ctypes.byref(bits), None, 0)
    if not hbmp:
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(None, screen)
        raise RuntimeError("CreateDIBSection failed")
    old = gdi32.SelectObject(hdc, hbmp)
    try:
        if not user32.PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT):
            raise RuntimeError("PrintWindow failed")
        raw = bytearray(ctypes.string_at(bits, w * h * 4))
    finally:
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(None, screen)

    rgba = bytearray(len(raw))
    rgba[0::4] = raw[2::4]
    rgba[1::4] = raw[1::4]
    rgba[2::4] = raw[0::4]
    rgba[3::4] = b"\xff" * (w * h)
    if not client_only:
        return w, h, rgba

    client = int(top.winfo_id())
    cr = wintypes.RECT()
    pt = wintypes.POINT(0, 0)
    if not (user32.GetClientRect(client, ctypes.byref(cr)) and user32.ClientToScreen(client, ctypes.byref(pt))):
        return w, h, rgba
    cx0, cy0 = max(0, pt.x - wr.left), max(0, pt.y - wr.top)
    cw, ch = min(cr.right, w - cx0), min(cr.bottom, h - cy0)
    if cw <= 0 or ch <= 0:
        return w, h, rgba
    out = bytearray(cw * ch * 4)
    for y in range(ch):
        s = ((cy0 + y) * w + cx0) * 4
        out[y * cw * 4:(y + 1) * cw * 4] = rgba[s:s + cw * 4]
    return cw, ch, out


def capture(widget: Any, path: str, client_only: bool = True) -> str:
    """Save the toplevel that holds `widget` as a PNG at `path`; returns the path."""
    w, h, rgba = grab(widget, client_only)
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as f:
        f.write(encode_png(w, h, bytes(rgba), 6))
    return path


def main(argv: Optional[list] = None) -> int:
    """Smoke test: python tools/snapshot.py out.png  (captures a tiny demo window)."""
    import tkinter as tk
    args = argv if argv is not None else sys.argv[1:]
    path = args[0] if args else "shots/snapshot_test.png"
    root = tk.Tk()
    root.geometry("240x120")
    tk.Label(root, text="snapshot test", font=("Segoe UI", 16)).pack(expand=True)
    root.after(400, lambda: (capture(root, path), root.destroy()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
