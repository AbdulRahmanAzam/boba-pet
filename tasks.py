"""To-do list, reminders and a focus guard for the desktop pet.

TaskStore keeps tasks in the pet's state file. FocusGuard watches which app
or browser tab is in front and escalates when you drift to a distraction
while tasks are still open: nudge, then nag, then (strict mode) minimize it.
During a focus session it reacts within seconds and always enforces.
Everything stays on this PC; nothing about your windows is sent anywhere.
"""

import ctypes
import re
import time
import uuid
from collections.abc import Callable
from ctypes import wintypes
from datetime import datetime

# Matched case-insensitively against the window title and the app's exe name.
DEFAULT_DISTRACTIONS = [
    "youtube", "netflix", "instagram", "facebook", "tiktok", "twitter",
    "reddit", "twitch", "prime video", "disney+", "9gag", "snapchat",
    "pinterest", "steam", "epic games", "roblox", "minecraft", "valorant",
    "league of legends", "pubg", "fortnite", "free fire",
]
PRETTY = {"youtube": "YouTube", "tiktok": "TikTok", "9gag": "9GAG",
          "pubg": "PUBG", "disney+": "Disney+"}
NUDGE_AFTER = 60  # seconds on a distraction before the first nudge
NAG_AFTER = 4 * 60  # ...before the stronger nag (speaks out loud)
ENFORCE_AFTER = 5 * 60  # ...before strict mode minimizes the window
SNOOZE_SECONDS = 10 * 60
# During a focus session the guard is always on, strict and unsnoozable.
FOCUS_NUDGE_AFTER = 15
FOCUS_NAG_AFTER = 45
FOCUS_ENFORCE_AFTER = 90

DUE = re.compile(r"\s*@\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)


class TaskStore:
    """Tasks live in the pet's state dict under "todos"."""

    def __init__(self, state: dict, save: Callable[[], None]) -> None:
        self.state = state
        self.save = save
        self.state.setdefault("todos", [])

    @property
    def items(self) -> list[dict]:
        return self.state["todos"]

    def pending(self) -> list[dict]:
        return [t for t in self.items if not t["done"]]

    def add(self, text: str) -> dict | None:
        """Add a task. A trailing "@17:30" or "@5pm" sets a due time today."""
        due = None
        m = DUE.search(text)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            if m.group(3):
                hour = hour % 12 + (12 if m.group(3).lower() == "pm" else 0)
            if 0 <= hour < 24 and 0 <= minute < 60:
                due = f"{hour:02d}:{minute:02d}"
                text = text[:m.start()]
        text = text.strip()
        if not text:
            return None
        task = {"id": uuid.uuid4().hex[:8], "text": text, "done": False,
                "created": time.time(), "due": due,
                "due_day": time.strftime("%Y-%m-%d") if due else None,
                "reminded": False}
        self.items.append(task)
        self.save()
        return task

    def toggle(self, task_id: str) -> dict | None:
        for t in self.items:
            if t["id"] == task_id:
                t["done"] = not t["done"]
                t["done_at"] = time.time() if t["done"] else None
                self.save()
                return t
        return None

    def delete(self, task_id: str) -> None:
        self.state["todos"] = [t for t in self.items if t["id"] != task_id]
        self.save()

    def clear_done(self) -> None:
        self.state["todos"] = self.pending()
        self.save()

    def ordered(self) -> list[dict]:
        """Open tasks first (soonest due first), finished ones after."""
        def key(t: dict) -> tuple:
            return (t["done"], t.get("due") or "99:99", t["created"])
        return sorted(self.items, key=key)

    def due_now(self) -> list[dict]:
        """Open tasks whose due time has just arrived (reported once)."""
        now = datetime.now()
        hits = []
        for t in self.pending():
            if not t.get("due") or t.get("reminded"):
                continue
            due = datetime.strptime(f"{t['due_day']} {t['due']}", "%Y-%m-%d %H:%M")
            if now >= due:
                t["reminded"] = True
                hits.append(t)
        if hits:
            self.save()
        return hits

    def summary(self, limit: int = 8) -> str:
        """Short list for Claude's system prompt."""
        items = self.pending()[:limit]
        return "; ".join(t["text"] + (f" (due {t['due']})" if t.get("due") else "")
                         for t in items)


# ---------- what is on screen ----------

def foreground_app() -> tuple[str, str, int]:
    """(window title, exe name, hwnd) of the window in front."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", "", 0
    length = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # query limited info
    if handle:
        size = wintypes.DWORD(520)
        buf = ctypes.create_unicode_buffer(520)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            exe = buf.value.rsplit("\\", 1)[-1]
        kernel32.CloseHandle(handle)
    return title.value, exe, hwnd


def display_name(match: str) -> str:
    return PRETTY.get(match, match.title())


def minimize(hwnd: int) -> None:
    ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE


class FocusGuard:
    """Tracks time spent on distractions and decides when to step in."""

    def __init__(self, distractions: list[str]) -> None:
        self.distractions = [d.lower() for d in distractions]
        self.since: float | None = None
        self.level = 0  # 0 none, 1 nudged, 2 nagged, 3 enforced
        self.snooze_until = 0.0
        self.current = ""

    def match(self, title: str, exe: str) -> str | None:
        hay = f"{title} {exe}".lower()
        if exe.lower() in ("python.exe", "pythonw.exe"):
            return None  # the pet itself
        return next((d for d in self.distractions if d in hay), None)

    def snooze(self) -> None:
        self.snooze_until = time.time() + SNOOZE_SECONDS
        self.since, self.level = None, 0

    def check(self, has_tasks: bool, strict: bool,
              focus: bool = False) -> tuple[int, str, int]:
        """Returns (action, distraction name, hwnd). action: 0 none,
        1 nudge, 2 nag, 3 minimize. focus=True uses the much shorter
        focus-session timings, always enforces and ignores snooze."""
        if focus:
            nudge, nag, enforce, rearm = (FOCUS_NUDGE_AFTER, FOCUS_NAG_AFTER,
                                          FOCUS_ENFORCE_AFTER, 20)
            has_tasks, strict = True, True
        else:
            nudge, nag, enforce, rearm = NUDGE_AFTER, NAG_AFTER, ENFORCE_AFTER, 60
        if not has_tasks or (not focus and time.time() < self.snooze_until):
            self.since, self.level = None, 0
            return 0, "", 0
        title, exe, hwnd = foreground_app()
        hit = self.match(title, exe)
        if not hit:
            self.since, self.level = None, 0
            return 0, "", 0
        now = time.time()
        if self.since is None or hit != self.current:
            self.since, self.level, self.current = now, 0, hit
        spent = now - self.since
        if spent >= enforce and strict and self.level >= 2:
            # minimize, then re-arm: still here a bit later, minimize again
            self.level = 2
            self.since = now - (enforce - rearm)
            return 3, hit, hwnd
        if spent >= nag and self.level < 2:
            self.level = 2
            return 2, hit, hwnd
        if spent >= nudge and self.level < 1:
            self.level = 1
            return 1, hit, hwnd
        return 0, hit, hwnd
