"""Focus timer visuals for the desktop pet.

FocusRing is a see-through window behind the pet: a thin ring that fills
clockwise as the session runs, a small time pill, and a confetti burst for
the finish. EdgeGlow is a click-through, per-pixel-alpha window covering the
screen that paints a soft purple haze along every edge while you focus.
"""

import ctypes
import math
import random
import time
import tkinter as tk
from ctypes import wintypes

from ui_fx import ease_in_cubic, ease_out_cubic, hwnd_of

FOCUS_SECONDS = 25 * 60
BREAK_SECONDS = 5 * 60

RING_FOCUS, RING_FOCUS_HEAD, RING_FOCUS_TRACK = "#A78BFA", "#F0ABFC", "#4A3A70"
RING_BREAK, RING_BREAK_HEAD, RING_BREAK_TRACK = "#6EE7B7", "#ECFDF5", "#24493E"
CONFETTI = ("#B9A3E3", "#F0ABFC", "#8B5CF6", "#6EE7B7", "#FDE68A", "#FCA5A5")
GLOW_RGB = (180, 140, 255)  # light purple


class FocusRing:
    """Progress ring drawn in its own window so it can extend past the pet."""

    CONFETTI_SECONDS = 2.2

    def __init__(self, root: tk.Tk, transparent: str, radius: int, width: int,
                 font: tuple, pill_bg: str, pill_fg: str) -> None:
        self.r = radius
        self.w = width
        self.pill_fg = pill_fg
        self.pad = round(radius * 0.9)  # room for confetti to fly
        self.size = 2 * (radius + self.pad)
        self.c = self.size / 2
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", transparent)
        self.win.configure(bg=transparent)
        self.win.withdraw()
        self.canvas = tk.Canvas(self.win, width=self.size, height=self.size,
                                bg=transparent, highlightthickness=0)
        self.canvas.pack()
        c, r = self.c, radius
        self.track = self.canvas.create_oval(c - r, c - r, c + r, c + r,
                                             outline=RING_FOCUS_TRACK, width=width)
        self.arc = self.canvas.create_arc(c - r, c - r, c + r, c + r, start=90,
                                          extent=0, style="arc",
                                          outline=RING_FOCUS, width=width)
        self.head = self.canvas.create_oval(0, 0, 0, 0, fill=RING_FOCUS_HEAD,
                                            outline="")
        # time pill sitting on the bottom of the ring
        ph = font[1] * 2 + 4
        pw = ph * 2.9
        py = c + r
        self.pill = [
            self.canvas.create_oval(c - pw / 2, py - ph / 2, c - pw / 2 + ph,
                                    py + ph / 2, fill=pill_bg, outline=""),
            self.canvas.create_oval(c + pw / 2 - ph, py - ph / 2, c + pw / 2,
                                    py + ph / 2, fill=pill_bg, outline=""),
            self.canvas.create_rectangle(c - pw / 2 + ph / 2, py - ph / 2,
                                         c + pw / 2 - ph / 2, py + ph / 2,
                                         fill=pill_bg, outline=""),
        ]
        self.label = self.canvas.create_text(c, py, text="", fill=pill_fg,
                                             font=font)
        self.shown = False
        self.drawn: tuple = ()
        self.particles: list[list] = []
        self.burst_at = 0.0

    def bind_click(self, fn) -> None:
        for item in (*self.pill, self.label):
            self.canvas.tag_bind(item, "<Button-1>", lambda _e: fn())
        self.canvas.configure(cursor="hand2")

    def place(self, cx: float, cy: float) -> None:
        """Center the ring on a screen point (the pet's sprite center)."""
        self.win.geometry(f"{self.size}x{self.size}+{round(cx - self.c)}"
                          f"+{round(cy - self.c)}")

    def show(self) -> None:
        if not self.shown:
            self.shown = True
            self.win.deiconify()

    def hide(self) -> None:
        self.shown = False
        self.drawn = ()
        if not self.particles:
            self.win.withdraw()

    def set(self, progress: float, text: str, on_break: bool,
            colors: tuple[str, str, str] | None = None) -> None:
        """progress 0..1 fills the ring clockwise from the top.

        colors: (arc, head, track) instead of the focus / break colors.
        """
        extent = round(-359.9 * min(max(progress, 0.0), 1.0), 1)
        state = (extent, text, on_break, colors)
        if state == self.drawn:
            return
        self.drawn = state
        color, head, track = colors or ((RING_BREAK, RING_BREAK_HEAD, RING_BREAK_TRACK)
                                        if on_break else
                                        (RING_FOCUS, RING_FOCUS_HEAD, RING_FOCUS_TRACK))
        cv = self.canvas
        cv.itemconfigure(self.track, outline=track)
        cv.itemconfigure(self.arc, extent=extent, outline=color)
        angle = math.radians(90 + extent)
        hx = self.c + self.r * math.cos(angle)
        hy = self.c - self.r * math.sin(angle)
        d = self.w * 1.3
        cv.coords(self.head, hx - d, hy - d, hx + d, hy + d)
        cv.itemconfigure(self.head, fill=head,
                         state="normal" if extent else "hidden")
        cv.itemconfigure(self.label, text=text,
                         fill=color if colors else
                         RING_BREAK if on_break else self.pill_fg)

    # ---------- celebration ----------
    def burst(self) -> None:
        """Confetti flies out from the ring."""
        self.win.deiconify()
        self.burst_at = time.perf_counter()
        for item in self.particles:
            self.canvas.delete(item[0])
        self.particles = []
        s = max(4, round(self.w * 2.2))
        for _ in range(46):
            a = random.uniform(0, 2 * math.pi)
            speed = random.uniform(0.9, 1.8) * self.pad
            x = self.c + self.r * math.cos(a)
            y = self.c + self.r * math.sin(a)
            item = self.canvas.create_rectangle(x, y, x + s, y + s,
                                                fill=random.choice(CONFETTI),
                                                outline="")
            self.particles.append([item, x, y, speed * math.cos(a),
                                   speed * math.sin(a) - 0.6 * self.pad])

    def step(self, now: float) -> bool:
        """Advance confetti. Returns True while it is still flying."""
        if not self.particles:
            return False
        t = now - self.burst_at
        if t > self.CONFETTI_SECONDS:
            for item in self.particles:
                self.canvas.delete(item[0])
            self.particles = []
            if not self.shown:
                self.win.withdraw()
            return False
        gravity = 2.4 * self.pad
        s = max(4, round(self.w * 2.2))
        for item, x, y, vx, vy in self.particles:
            px = x + vx * t
            py = y + vy * t + 0.5 * gravity * t * t
            self.canvas.coords(item, px, py, px + s, py + s)
        if t > self.CONFETTI_SECONDS * 0.7:  # flicker out at the end
            on = int(t * 14) % 2 == 0
            for item in self.particles:
                self.canvas.itemconfigure(item[0],
                                          state="normal" if on else "hidden")
        return True


# ---------- Win32: layered, click-through window ----------

class _BlendFunction(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TRANSPARENT = 0x8, 0x20
WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80, 0x80000, 0x8000000
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

# Own DLL handles so these argtypes never clash with other ctypes users.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = wintypes.LONG
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
_user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    wintypes.COLORREF, ctypes.POINTER(_BlendFunction), wintypes.DWORD]
_user32.UpdateLayeredWindow.restype = wintypes.BOOL
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                    ctypes.POINTER(ctypes.c_void_p),
                                    wintypes.HANDLE, wintypes.DWORD]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ


def edge_pixels(w: int, h: int, band: int, rgb: tuple[int, int, int],
                max_alpha: int) -> bytes:
    """Top-down premultiplied BGRA: color fading in from every screen edge."""
    r, g, b = rgb
    cache: dict[int, bytes] = {}

    def px(frac: float) -> bytes:
        a = round(frac * max_alpha)
        if a not in cache:
            cache[a] = bytes((b * a // 255, g * a // 255, r * a // 255, a))
        return cache[a]

    falloff = [(1 - d / band) ** 2.6 for d in range(band)]
    mid = w - 2 * band
    rows: dict[int, bytes] = {}
    out = []
    for y in range(h):
        dy = min(y, h - 1 - y, band)
        if dy not in rows:
            ay = falloff[dy] if dy < band else 0.0
            # corners: the two edges add up like overlapping light
            side = [px(1 - (1 - ay) * (1 - fx)) for fx in falloff]
            rows[dy] = b"".join(side) + px(ay) * mid + b"".join(reversed(side))
        out.append(rows[dy])
    return b"".join(out)


class EdgeGlow:
    """Soft purple haze along all screen edges. Click-through, never focused."""

    BREATH_SECONDS = 4.0

    def __init__(self, root: tk.Tk, band: int, max_alpha: int = 165) -> None:
        self.root = root
        self.band = band
        self.max_alpha = max_alpha
        self.win: tk.Toplevel | None = None
        self.hwnd = 0
        self.screen_dc = None
        self.mem_dc = None
        self.size = (0, 0)
        self.visible = False
        self.level = 0.0
        self.fades: list[tuple[float, float, float]] = []  # (to, seconds, ease)
        self.fade_from = (0.0, 0.0)  # (level at start, start time)
        self.alpha_drawn = -1
        self.t0 = time.perf_counter()

    def _build(self) -> bool:
        if self.win is not None:
            return bool(self.hwnd)
        try:
            self.win = tk.Toplevel(self.root)
            self.win.overrideredirect(True)
            self.win.withdraw()
            self.win.attributes("-topmost", True)
            w = _user32.GetSystemMetrics(0)
            h = _user32.GetSystemMetrics(1)
            self.win.geometry(f"{w}x{h}+0+0")
            hwnd = hwnd_of(self.win)
            ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                   ex | WS_EX_LAYERED | WS_EX_TRANSPARENT
                                   | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
                                   | WS_EX_TOPMOST)
            band = min(self.band, w // 3, h // 3)
            pixels = edge_pixels(w, h, band, GLOW_RGB, self.max_alpha)
            header = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), w, -h,
                                       1, 32, 0, 0, 0, 0, 0, 0)
            self.screen_dc = _user32.GetDC(None)
            self.mem_dc = _gdi32.CreateCompatibleDC(self.screen_dc)
            bits = ctypes.c_void_p()
            bitmap = _gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(header), 0,
                                             ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                return False
            ctypes.memmove(bits.value, pixels, len(pixels))
            _gdi32.SelectObject(self.mem_dc, bitmap)
            self.hwnd = hwnd
            self.size = (w, h)
            self._push(0, full=True)
            return True
        except (AttributeError, OSError, tk.TclError):
            self.hwnd = 0
            return False

    def _push(self, alpha: int, full: bool = False) -> None:
        blend = _BlendFunction(0, 0, alpha, AC_SRC_ALPHA)
        if full:
            dst = wintypes.POINT(0, 0)
            src = wintypes.POINT(0, 0)
            size = wintypes.SIZE(*self.size)
            _user32.UpdateLayeredWindow(self.hwnd, self.screen_dc,
                                        ctypes.byref(dst), ctypes.byref(size),
                                        self.mem_dc, ctypes.byref(src), 0,
                                        ctypes.byref(blend), ULW_ALPHA)
        else:  # shape unchanged: only the overall opacity moves
            _user32.UpdateLayeredWindow(self.hwnd, None, None, None, None, None,
                                        0, ctypes.byref(blend), ULW_ALPHA)
        self.alpha_drawn = alpha

    def _fade(self, *steps: tuple[float, float, float]) -> None:
        self.fades = list(steps)
        self.fade_from = (self.level, time.perf_counter())

    def show(self) -> None:
        if not self._build():
            return
        if not self.visible:
            self.visible = True
            _user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self._fade((1.0, 1.2, 1))

    def hide(self) -> None:
        if self.visible:
            self._fade((0.0, 0.9, 0))

    def celebrate(self) -> None:
        """Bright flash, then the glow melts away."""
        if self.visible:
            self._fade((1.6, 0.25, 1), (0.0, 1.8, 0))

    def raise_(self) -> None:
        if self.visible:
            _user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def animating(self) -> bool:
        return bool(self.fades)

    def step(self, now: float) -> None:
        if not self.visible:
            return
        if self.fades:
            to, dur, rising = self.fades[0]
            start_level, start = self.fade_from
            p = min((now - start) / dur, 1.0)
            ease = ease_out_cubic if rising else ease_in_cubic
            self.level = start_level + (to - start_level) * ease(p)
            if p >= 1:
                self.fades.pop(0)
                self.fade_from = (self.level, now)
                if not self.fades and self.level <= 0:
                    self.visible = False
                    self._push(0)
                    _user32.ShowWindow(self.hwnd, SW_HIDE)
                    return
        phase = (now - self.t0) * 2 * math.pi / self.BREATH_SECONDS
        breath = 0.7 + 0.3 * (0.5 + 0.5 * math.sin(phase))
        alpha = max(0, min(255, round(255 * self.level * breath)))
        if alpha != self.alpha_drawn:
            self._push(alpha)
