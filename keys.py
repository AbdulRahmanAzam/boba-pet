"""Keystroke counter: how many keys you press each day, and in total.

Only a number is kept. A low-level keyboard hook bumps a counter on each
new key press; which key it was is used for nothing except skipping
auto-repeat (a held key counts once) and is never stored, logged or sent
anywhere. Keys typed by software (the pet's own computer tools, macros)
are not counted.

Stored in the pet's state file under "keys":
    {"total": 123456, "days": {"2026-10-10": 8123, ...}, "best": ["2026-10-02", 15000]}
"days" keeps the last DAYS_KEPT days; "total" holds every day ever counted.
"""

import ctypes
import threading
import time
from ctypes import wintypes

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
LLKHF_INJECTED = 0x10
DAYS_KEPT = 60
MILESTONE = 100_000  # celebrate every 100k keys in total


class _KbdLL(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM,
                               wintypes.LPARAM)


class Counter:
    """Counts key presses on a background thread; take() hands them over."""

    def __init__(self) -> None:
        self.pending = 0
        self.lock = threading.Lock()
        self.down: set[int] = set()  # keys held right now (auto-repeat filter)
        self.thread: threading.Thread | None = None
        self.thread_id = 0
        self._proc = _HOOKPROC(self._on_key)  # keep a reference for Windows

    def start(self) -> bool:
        if self.thread:
            return True
        ready = threading.Event()
        ok = [False]

        def run() -> None:
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.SetWindowsHookExW.argtypes = (ctypes.c_int, _HOOKPROC,
                                                 wintypes.HINSTANCE, wintypes.DWORD)
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int,
                                              wintypes.WPARAM, wintypes.LPARAM)
            self.thread_id = kernel32.GetCurrentThreadId()
            hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
            ok[0] = bool(hook)
            ready.set()
            if not hook:
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
            user32.UnhookWindowsHookEx(hook)

        self.thread = threading.Thread(target=run, daemon=True, name="key-counter")
        self.thread.start()
        ready.wait(3)
        if not ok[0]:
            self.thread = None
        return ok[0]

    def stop(self) -> None:
        if self.thread and self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
        self.thread = None

    def _on_key(self, code: int, wparam: int, lparam: int) -> int:
        if code == 0:
            info = ctypes.cast(lparam, ctypes.POINTER(_KbdLL)).contents
            if not info.flags & LLKHF_INJECTED:
                if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    if info.vkCode not in self.down:
                        self.down.add(info.vkCode)
                        with self.lock:
                            self.pending += 1
                elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                    self.down.discard(info.vkCode)
        return ctypes.windll.user32.CallNextHookEx(None, code, wparam, lparam)

    def take(self) -> int:
        with self.lock:
            n, self.pending = self.pending, 0
        return n


def today() -> str:
    return time.strftime("%Y-%m-%d")


def stats(state: dict) -> dict:
    return state.setdefault("keys", {"total": 0, "days": {}, "best": ["", 0]})


def add(state: dict, n: int) -> tuple[bool, bool]:
    """Add n presses to today and the total.

    Returns (new_record_today, milestone_crossed): the first time today
    beats the best day so far, and when the total passes a MILESTONE.
    """
    if n <= 0:
        return False, False
    k = stats(state)
    day = today()
    before_total = k.get("total", 0)
    k["total"] = before_total + n
    k["days"][day] = k["days"].get(day, 0) + n
    for old in sorted(k["days"])[:-DAYS_KEPT]:
        del k["days"][old]
    best_day, best = k.get("best") or ["", 0]
    record = False
    if k["days"][day] > best:
        record = best > 0 and best_day != day  # beat an older day's record
        k["best"] = [day, k["days"][day]]
    milestone = before_total // MILESTONE < k["total"] // MILESTONE
    return record, milestone


def today_count(state: dict) -> int:
    return stats(state)["days"].get(today(), 0)


def short(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def summary(state: dict) -> str:
    """Markdown for the stats bubble."""
    k = stats(state)
    days = k["days"]
    last7 = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
             for i in range(6, -1, -1)]
    top = max([days.get(d, 0) for d in last7] + [1])
    bars = "\n".join(
        f"`{time.strftime('%a', time.strptime(d, '%Y-%m-%d'))} "
        f"{'█' * round(10 * days.get(d, 0) / top):<10}` {days.get(d, 0):,}"
        for d in last7)
    best_day, best = k.get("best") or ["", 0]
    best_line = (f"\nBest day: **{best:,}** "
                 f"({time.strftime('%d %b', time.strptime(best_day, '%Y-%m-%d'))})"
                 if best_day else "")
    return (f"⌨ **Today: {today_count(state):,} keys**\n"
            f"All time: **{k.get('total', 0):,}**{best_line}\n\n{bars}")
