"""Screen snipping for the pet: drag a box over the screen, save it as PNG.

The overlay dims every monitor; the box you drag is cut out of the dim layer
(color-key transparent), so the selected area shows at full brightness.
Capture uses GDI BitBlt and the PNG is written with zlib, so no extra
packages are needed.
"""

import ctypes
import struct
import time
import tkinter as tk
import zlib
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

HOLE = "#010203"  # color key: pixels of this color are see-through
DIM_ALPHA = 0.38
MIN_SIZE = 8  # smaller drags count as a click: cancel
KEEP_SHOTS = 20


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, width, height) spanning all monitors, in physical pixels."""
    m = ctypes.windll.user32.GetSystemMetrics
    return m(76), m(77), m(78), m(79)


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def grab(x: int, y: int, w: int, h: int) -> bytes:
    """Screen pixels of a rectangle as top-down BGRA rows."""
    # private DLL handles: argtypes set here don't leak into other modules
    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    gdi32.SelectObject(dc, bmp)
    gdi32.BitBlt(dc, 0, 0, w, h, screen, x, y, 0x00CC0020 | 0x40000000)  # SRCCOPY|CAPTUREBLT
    info = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), w, -h, 1, 32, 0,
                             0, 0, 0, 0, 0)  # negative height: top-down rows
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(info), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, screen)
    return buf.raw


def png_bytes(bgra: bytes, w: int, h: int) -> bytes:
    """Encode BGRA pixels as an RGB PNG."""
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = w * 3
    raw = b"".join(b"\x00" + bytes(rgb[r * stride:(r + 1) * stride]) for r in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def save_shot(folder: Path, x: int, y: int, w: int, h: int) -> Path:
    """Capture a screen rectangle to folder/shot-<time>.png (keeps the last few)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / time.strftime("shot-%Y%m%d-%H%M%S.png")
    path.write_bytes(png_bytes(grab(x, y, w, h), w, h))
    for old in sorted(folder.glob("shot-*.png"))[:-KEEP_SHOTS]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


class SnipOverlay:
    """Full-screen dim layer; drag a box, Esc or right-click cancels."""

    def __init__(self, root: tk.Tk, accent: str, font: tuple,
                 on_done: Callable[[tuple[int, int, int, int] | None], None]) -> None:
        self.on_done = on_done
        self.vx, self.vy, vw, vh = virtual_screen()
        self.start: tuple[int, int] | None = None
        win = self.win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", DIM_ALPHA)
        win.attributes("-transparentcolor", HOLE)
        win.geometry(f"{vw}x{vh}+{self.vx}+{self.vy}")
        c = self.canvas = tk.Canvas(win, bg="black", highlightthickness=0,
                                    cursor="crosshair")
        c.pack(fill="both", expand=True)
        self.box = c.create_rectangle(0, 0, 0, 0, fill=HOLE, outline=accent,
                                      width=2, state="hidden")
        px, py = root.winfo_pointerxy()
        self.hint = c.create_text(px - self.vx, py - self.vy + 34, fill="white",
                                  font=(font[0], 11, "bold"),
                                  text="Drag to capture  ·  Esc to cancel")
        c.bind("<ButtonPress-1>", self.press)
        c.bind("<B1-Motion>", self.drag)
        c.bind("<ButtonRelease-1>", self.release)
        c.bind("<Button-3>", lambda _e: self.finish(None))
        win.bind("<Escape>", lambda _e: self.finish(None))
        win.after(10, self.grab_focus)

    def grab_focus(self) -> None:
        try:
            ctypes.windll.user32.SetForegroundWindow(
                ctypes.windll.user32.GetParent(self.win.winfo_id()))
        except OSError:
            pass
        self.win.focus_force()

    def press(self, e: tk.Event) -> None:
        self.start = (e.x, e.y)
        self.canvas.itemconfigure(self.hint, state="hidden")
        self.canvas.coords(self.box, e.x, e.y, e.x, e.y)
        self.canvas.itemconfigure(self.box, state="normal")

    def drag(self, e: tk.Event) -> None:
        if self.start:
            self.canvas.coords(self.box, *self.start, e.x, e.y)

    def release(self, e: tk.Event) -> None:
        if not self.start:
            return
        x0, x1 = sorted((self.start[0], e.x))
        y0, y1 = sorted((self.start[1], e.y))
        if x1 - x0 < MIN_SIZE or y1 - y0 < MIN_SIZE:
            self.finish(None)
        else:
            self.finish((x0 + self.vx, y0 + self.vy, x1 - x0, y1 - y0))

    def finish(self, rect: tuple[int, int, int, int] | None) -> None:
        self.win.destroy()
        self.on_done(rect)
