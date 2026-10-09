"""UI polish helpers: Windows 11 window styling, fades, easing, markdown.

Kept separate from claude_pet.py so the app file stays about behaviour.
"""

import ctypes
import re
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from collections.abc import Callable

# ---------- easing ----------


def ease_out_cubic(p: float) -> float:
    return 1 - (1 - p) ** 3


def ease_in_cubic(p: float) -> float:
    return p ** 3


def lerp_color(a: str, b: str, p: float) -> str:
    """Blend two #rrggbb colors (p=0 gives a, p=1 gives b)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * p):02x}" for x, y in zip(ca, cb))


# ---------- Windows: DPI + Windows 11 window chrome ----------


def enable_dpi_awareness() -> float:
    """Crisp text on scaled displays. Returns the scale (1.0 at 100%)."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            return 1.0
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96
    except (AttributeError, OSError):
        return 1.0


def hwnd_of(win: tk.Misc) -> int:
    win.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
    return hwnd or win.winfo_id()


DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2


def _colorref(hex_color: str) -> int:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def round_corners(win: tk.Misc, border: str | None = None) -> None:
    """Windows 11 rounded corners + thin colored border (no-op elsewhere)."""
    try:
        hwnd = hwnd_of(win)
        dwm = ctypes.windll.dwmapi
        pref = ctypes.c_int(DWMWCP_ROUND)
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                  ctypes.byref(pref), ctypes.sizeof(pref))
        if border:
            set_border(win, border)
    except (AttributeError, OSError):
        pass


def set_border(win: tk.Misc, color: str) -> None:
    try:
        ref = ctypes.c_uint(_colorref(color))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd_of(win), DWMWA_BORDER_COLOR, ctypes.byref(ref), ctypes.sizeof(ref))
    except (AttributeError, OSError):
        pass


# ---------- fade / slide for popup windows ----------


class Fader:
    """Fade + short slide for borderless Toplevels.

    `shown(win)` is the logical state: a window that is fading out already
    counts as hidden, so toggles never fight an animation in progress.
    """

    SHOW_MS = 150
    HIDE_MS = 110
    SLIDE_PX = 8

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.visible: set[tk.Toplevel] = set()
        self.jobs: dict[tk.Toplevel, str] = {}
        self.targets: dict[tk.Toplevel, tuple[int, int]] = {}

    def shown(self, win: tk.Toplevel) -> bool:
        return win in self.visible

    def place(self, win: tk.Toplevel, x: int, y: int) -> None:
        """Set the resting position (respected mid-animation too)."""
        self.targets[win] = (x, y)
        if win not in self.jobs:
            win.geometry(f"+{x}+{y}")

    def _cancel(self, win: tk.Toplevel) -> None:
        job = self.jobs.pop(win, None)
        if job:
            self.root.after_cancel(job)

    def show(self, win: tk.Toplevel, slide: int = 1) -> None:
        """slide=+1 rises into place, -1 drops into place, 0 fade only."""
        if win in self.visible and win not in self.jobs:
            win.lift()
            return
        self._cancel(win)
        self.visible.add(win)
        start_alpha = float(win.attributes("-alpha")) if win.winfo_viewable() else 0.0
        win.attributes("-alpha", start_alpha)
        win.deiconify()
        win.lift()
        t0 = time.perf_counter()

        def step() -> None:
            p = min((time.perf_counter() - t0) * 1000 / self.SHOW_MS, 1.0)
            e = ease_out_cubic(p)
            win.attributes("-alpha", start_alpha + (1 - start_alpha) * e)
            x, y = self.targets.get(win, (win.winfo_x(), win.winfo_y()))
            win.geometry(f"+{x}+{round(y + slide * self.SLIDE_PX * (1 - e))}")
            if p < 1:
                self.jobs[win] = self.root.after(8, step)
            else:
                self.jobs.pop(win, None)

        step()

    def hide(self, win: tk.Toplevel, on_done: Callable[[], None] | None = None) -> None:
        if win not in self.visible:
            return
        self._cancel(win)
        self.visible.discard(win)
        if not win.winfo_viewable():
            return
        start_alpha = float(win.attributes("-alpha"))
        t0 = time.perf_counter()

        def step() -> None:
            p = min((time.perf_counter() - t0) * 1000 / self.HIDE_MS, 1.0)
            win.attributes("-alpha", start_alpha * (1 - ease_in_cubic(p)))
            if p < 1:
                self.jobs[win] = self.root.after(8, step)
            else:
                self.jobs.pop(win, None)
                win.withdraw()
                if on_done:
                    on_done()

        step()


# ---------- markdown into a Text widget ----------

CODE_FONT = ("Cascadia Mono", 9)


def pick_font(*families: str) -> str:
    available = set(tkfont.families())
    return next((f for f in families if f in available), families[-1])


def setup_markdown(widget: tk.Text, fg: str, muted: str, accent: str,
                   code_bg: str, base_font: tuple) -> None:
    family, size = base_font[0], base_font[1]
    mono = pick_font("Cascadia Mono", "Consolas", "Courier New")
    widget.tag_configure("b", font=(family, size, "bold"))
    widget.tag_configure("i", font=(family, size, "italic"))
    widget.tag_configure("bi", font=(family, size, "bold italic"))
    widget.tag_configure("h", font=(family, size + 1, "bold"), foreground=fg,
                         spacing1=4, spacing3=2)
    widget.tag_configure("code", font=(mono, size - 1), background=code_bg,
                         foreground="#D9CCF5")
    widget.tag_configure("pre", font=(mono, size - 1), background=code_bg,
                         foreground=fg, lmargin1=8, lmargin2=8, rmargin=8,
                         spacing1=0, spacing3=0)
    widget.tag_configure("li", lmargin1=4, lmargin2=18)
    widget.tag_configure("bullet", foreground=accent)
    widget.tag_configure("quote", foreground=muted, lmargin1=10, lmargin2=10)
    widget.tag_configure("link", foreground="#8AB4F8", underline=True)
    widget.tag_bind("link", "<Enter>", lambda _e: widget.configure(cursor="hand2"))
    widget.tag_bind("link", "<Leave>", lambda _e: widget.configure(cursor="arrow"))
    widget.tag_configure("hidden", elide=True)


INLINE = re.compile(
    r"(\*\*\*(.+?)\*\*\*)|(\*\*(.+?)\*\*)|(__(.+?)__)|(`([^`]+)`)"
    r"|(\[([^\]]+)\]\((https?://[^)\s]+)\))|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"
    r"|(https?://[^\s)]+)")


def _insert_inline(widget: tk.Text, text: str, tags: tuple) -> None:
    pos = 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            widget.insert("end", text[pos:m.start()], tags)
        if m.group(2):
            widget.insert("end", m.group(2), tags + ("bi",))
        elif m.group(4) or m.group(6):
            widget.insert("end", m.group(4) or m.group(6), tags + ("b",))
        elif m.group(8):
            widget.insert("end", f" {m.group(8)} ", tags + ("code",))
        elif m.group(10):
            _insert_link(widget, m.group(10), m.group(11), tags)
        elif m.group(12):
            widget.insert("end", m.group(12), tags + ("i",))
        elif m.group(13):
            _insert_link(widget, m.group(13), m.group(13), tags)
        pos = m.end()
    if pos < len(text):
        widget.insert("end", text[pos:], tags)


def _insert_link(widget: tk.Text, label: str, url: str, tags: tuple) -> None:
    name = f"link-{widget.index('end')}"
    widget.insert("end", label, tags + ("link", name))
    widget.tag_bind(name, "<Button-1>", lambda _e: webbrowser.open(url))


def render_markdown(widget: tk.Text, md: str, base: tuple = ()) -> str:
    """Append markdown to a Text widget with tags. Returns the plain text."""
    lines = md.replace("\r\n", "\n").split("\n")
    in_code = False
    plain: list[str] = []
    for n, line in enumerate(lines):
        last = n == len(lines) - 1
        nl = "" if last else "\n"
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            widget.insert("end", line + nl, base + ("pre",))
            plain.append(line)
            continue
        heading = re.match(r"^\s*#{1,6}\s+(.*)", line)
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        number = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        quote = re.match(r"^\s*>\s?(.*)", line)
        if heading:
            _insert_inline(widget, heading.group(1), base + ("h",))
            plain.append(heading.group(1))
        elif bullet:
            widget.insert("end", "•  ", base + ("li", "bullet"))
            _insert_inline(widget, bullet.group(2), base + ("li",))
            plain.append("•  " + bullet.group(2))
        elif number:
            widget.insert("end", f"{number.group(2)}.  ", base + ("li", "bullet"))
            _insert_inline(widget, number.group(3), base + ("li",))
            plain.append(f"{number.group(2)}.  {number.group(3)}")
        elif quote:
            _insert_inline(widget, quote.group(1), base + ("quote",))
            plain.append(quote.group(1))
        else:
            _insert_inline(widget, line, base)
            plain.append(line)
        widget.insert("end", nl, base)
    return "\n".join(plain)
