"""Hackathons for the desktop pet, shared with the hack-clock Claude Code mod.

Both read and write %USERPROFILE%\\.hackclock\\hackathons.json, so an event
added, marked or removed here shows on the strip above the Claude Code prompt
within a few seconds, and the other way round. Countdowns and progress are
worked out locally. Adding a link runs the mod's own `/hack add <url>` in a
headless Claude Code, which makes the single Claude Haiku 5.5 extraction call
and saves the result; a link already saved is reused without a model call.
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_FILE = Path.home() / ".hackclock" / "hackathons.json"
URGENT_SECONDS = 6 * 3600
CREATE_NO_WINDOW = 0x08000000
ADD_TIMEOUT_SECONDS = 180
# The add runs in a bare Claude Code: only the hack-clock mod, no other plugins.
ADD_SETTINGS = json.dumps({"enabledPlugins": {
    "design@synced": False,
    "cowork-plugin-management@synced": False,
    "caveman@caveman": False,
    "savvy-flow@azam-local": False,
    "hack-clock@azam-local": True,
}})


def _instant(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read() -> list[dict]:
    """The saved list; raises when the file is unreadable or mid-write."""
    events = json.loads(DATA_FILE.read_text(encoding="utf-8")).get("events")
    if not isinstance(events, list):
        raise ValueError("hackathons.json has no events list")
    return events


def load() -> list[dict]:
    try:
        return _read()
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        time.sleep(0.05)  # the mod may be mid-write; one retry
        try:
            return _read()
        except (OSError, ValueError):
            return []


def _edit(change) -> None:
    """Apply change to the latest list; never writes over a file it could not read."""
    try:
        events = _read()
    except FileNotFoundError:
        events = []
    except (OSError, ValueError):
        time.sleep(0.05)
        events = _read()
    save(change(events))


def save(events: list[dict]) -> None:
    """Write the list the way the mod does, through a temp file so a reader never sees half."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "events": events}, indent=2,
                              ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def update(event_id: str, **change: object) -> None:
    """Change one event in the file's latest list (the mod may have written it)."""
    _edit(lambda events: [{**e, **change} if e.get("id") == event_id else e
                          for e in events])


def remove(event_id: str) -> None:
    _edit(lambda events: [e for e in events if e.get("id") != event_id])


def mtime() -> float:
    try:
        return DATA_FILE.stat().st_mtime
    except OSError:
        return 0.0


def phase(event: dict, now: float) -> tuple[str, float | None]:
    """Where an event stands: (kind, deadline), the same rules as the mod.

    Kinds: register, starts, submit, submitted, missing, closed.
    """
    if event.get("isSubmitted"):
        return "submitted", None
    reg = _instant(event.get("registrationDeadline"))
    start = _instant(event.get("start"))
    sub = _instant(event.get("submissionDeadline"))
    if not event.get("isRegistered"):
        if reg is not None and now < reg:
            return "register", reg
        if reg is None and (start is None or now < start):
            return "missing", None
        if reg is not None:
            return "closed", None
    if start is not None and now < start:
        return "starts", start
    if sub is None:
        return "missing", None
    if now < sub:
        return "submit", sub
    return "closed", None


RANK = {"register": 0, "submit": 0, "starts": 1, "missing": 2, "submitted": 3, "closed": 4}


def ranked(events: list[dict], now: float) -> list[tuple[dict, str, float | None]]:
    """Nearest actionable deadline first, as on the strip."""
    rows = [(e, *phase(e, now)) for e in events]
    return sorted(rows, key=lambda r: (RANK[r[1]], r[2] or 0))


def duration(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    days, hours, mins = minutes // 1440, minutes // 60 % 24, minutes % 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    days, rest = divmod(s, 86400)
    text = time.strftime("%H:%M:%S", time.gmtime(rest))
    return f"{days}d {text}" if days else text


def countdown(event: dict, kind: str, at: float | None, now: float) -> str:
    if kind == "register" and at:
        return f"Register in {duration(at - now)}"
    if kind == "starts" and at:
        when = datetime.fromtimestamp(at).strftime("%d %b, %I:%M %p").replace(" 0", " ")
        return f"Starts {when.lstrip('0')}"
    if kind == "submit" and at:
        return f"Submission closes in {clock(at - now)}"
    if kind == "submitted":
        return "Submitted ✓"
    if kind == "missing":
        return "Set the dates"
    return "Registration closed" if not event.get("isRegistered") else "Submission closed"


def is_urgent(kind: str, at: float | None, now: float) -> bool:
    return kind in ("register", "submit") and at is not None and at - now < URGENT_SECONDS


def progress(event: dict, kind: str, at: float | None, now: float) -> float | None:
    """How far through the current stretch, 0 to 1, as the strip's bar."""
    if kind == "submitted":
        return 1.0
    if kind not in ("register", "starts", "submit") or at is None:
        return None
    added = _instant(event.get("addedAt")) or now
    if kind == "register":
        begin = added
    elif kind == "starts":
        begin = min(_instant(event.get("registrationDeadline")) or added, added)
    else:
        begin = _instant(event.get("start")) or added
    if not (begin < at and begin <= now):
        begin = min(now, at - 86400)
    span = at - begin
    return 1.0 if span <= 0 else min(1.0, max(0.0, (now - begin) / span))


def link_of(event: dict, kind: str) -> str:
    links = event.get("links") or {}
    if kind == "register" and links.get("registration"):
        return links["registration"]
    if kind == "submit" and links.get("submission"):
        return links["submission"]
    return event.get("url", "")


def _decode(raw: bytes) -> str:
    """Claude Code writes UTF-8, but a piped Windows console may hand back the ANSI code page."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def is_blocked(message: str) -> bool:
    """The mod's answer when a site's bot check stopped every automatic route."""
    return "/hack paste" in message


def split_entry(text: str) -> tuple[str, str] | None:
    """(link, focus) from what was typed: a link plus, optionally, words naming
    one competition on a page that lists several (before or after the link)."""
    found = re.search(r"https?://\S+", text, re.I)
    if not found:
        return None
    focus = (text[:found.start()] + " " + text[found.end():]).strip(" \t-–:,")
    return found.group(0), " ".join(focus.split())


def add_url(claude: str, url: str, pasted: bool = False,
            focus: str = "") -> tuple[bool, str]:
    """Run the mod's `/hack add <url> [focus]` headless, or `/hack paste <url>
    [focus]` to read the page text the person copied; returns (ok, its message).
    `focus` picks one competition from a page that lists several; the same
    single Haiku call matches it by meaning."""
    verb = "paste" if pasted else "add"
    args = f"{url} {focus}".strip()
    try:
        done = subprocess.run(
            [claude, "-p", f"/hack {verb} {args}", "--settings", ADD_SETTINGS],
            capture_output=True, timeout=ADD_TIMEOUT_SECONDS, cwd=str(Path.home()),
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not run Claude Code: {exc}"
    text = _decode(done.stdout or done.stderr).strip()
    text = text.removeprefix("hack-clock:").strip()
    ok = done.returncode == 0 and ("Saved" in text or "Already saved" in text)
    return ok, text or "No answer from Claude Code."


def utc_now() -> float:
    return datetime.now(timezone.utc).timestamp()
