"""Hourly water reminder: a short alarm chime, a spoken nudge and a daily
glass counter.

The timer counts from the last glass you confirmed (or from when the pet
started). While you are away from the PC (no mouse or keyboard input), the
reminder waits and fires when you are back, so it never rings into an
empty room.
"""

import ctypes
import threading
import time
import winsound

INTERVAL_SECONDS = 60 * 60  # one glass every hour
SNOOZE_SECONDS = 10 * 60
AWAY_SECONDS = 5 * 60  # no input for this long counts as away
DAILY_GOAL = 8  # glasses
# Rising chime, played twice: C6 E6 G6 C7.
CHIME = ((1047, 140), (1319, 140), (1568, 140), (2093, 260))
SPOKEN = "Time to drink water! Please drink one glass of water now."


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Seconds since the last mouse or keyboard input on this PC."""
    info = _LastInput(ctypes.sizeof(_LastInput), 0)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000
    except (AttributeError, OSError):
        return 0.0


def _play() -> None:
    try:
        for _ in range(2):
            for freq, ms in CHIME:
                winsound.Beep(freq, ms)
            time.sleep(0.25)
    except RuntimeError:  # no sound device: the bubble and voice still show
        pass


def chime() -> None:
    """Play the alarm without blocking the Tk thread."""
    threading.Thread(target=_play, daemon=True).start()


def chime_seconds() -> float:
    return 2 * (sum(ms for _, ms in CHIME) / 1000 + 0.25)


def today() -> str:
    return time.strftime("%Y-%m-%d")


def glasses_today(state: dict) -> int:
    return state.get("water_glasses", 0) if state.get("water_day") == today() else 0


def add_glass(state: dict) -> int:
    """Count one glass for today; returns today's total."""
    n = glasses_today(state) + 1
    state["water_day"], state["water_glasses"] = today(), n
    return n


def due(next_at: float, now: float, idle: float) -> bool:
    return now >= next_at and idle < AWAY_SECONDS
