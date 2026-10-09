"""Claude Code hook: tells the desktop pet when a session finishes or needs you.

Installed as an async Stop + Notification hook in Claude Code settings. It
reads the hook JSON from stdin and appends one line to EVENTS_FILE, which
the pet watches. Uses only the standard library and never fails loudly, so
it can't get in Claude Code's way.
"""

import ctypes
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

EVENTS_FILE = Path.home() / ".claude-pet-events.jsonl"
TAIL_BYTES = 256 * 1024  # only the end of the transcript is needed
SNIPPET_CHARS = 160


def read_tail(path: str) -> list[dict]:
    """Parse the last chunk of a JSONL transcript (newest entries last)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    entries = []
    for line in raw.splitlines()[1 if size > TAIL_BYTES else 0:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def last_turn(entries: list[dict]) -> tuple[str, float | None]:
    """(last assistant text, seconds since the user's last real prompt)."""
    text, started = "", None
    for e in reversed(entries):
        content = (e.get("message") or {}).get("content")
        if not text and e.get("type") == "assistant" and isinstance(content, list):
            parts = [b.get("text", "") for b in content if b.get("type") == "text"]
            text = " ".join(" ".join(parts).split())
        if e.get("type") == "user" and isinstance(content, str) and not e.get("isMeta"):
            try:
                ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                started = time.time() - ts.timestamp()
            except (KeyError, ValueError):
                pass
            break
    if len(text) > SNIPPET_CHARS:
        text = text[:SNIPPET_CHARS - 1].rstrip() + "…"
    return text, started


class _Entry(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_char * 260)]


def ancestor_pids(limit: int = 12) -> list[int]:
    """Parent chain of this hook (claude, shell, terminal / VS Code...).

    The pet uses it to find the window this Claude Code session lives in.
    """
    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        parent: dict[int, int] = {}
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            parent[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = k32.Process32Next(snap, ctypes.byref(entry))
        k32.CloseHandle(snap)
    except (AttributeError, OSError):
        return []
    chain, pid = [], os.getpid()
    while pid in parent and len(chain) < limit:
        pid = parent[pid]
        if not pid or pid in chain:
            break
        chain.append(pid)
    return chain


def account_name() -> str:
    """Which Claude account this is: ~/.claude-work gives "work".

    Each account is a separate config dir picked via CLAUDE_CONFIG_DIR;
    the plain ~/.claude account gives "".
    """
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or ".claude").name
    return folder.removeprefix(".claude").lstrip("-_")


def main() -> None:
    if os.environ.get("CLAUDE_PET_CHILD"):
        return  # the pet's own `claude -p` calls: don't alert about ourselves
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    kind = data.get("hook_event_name", "")
    if kind not in ("Stop", "Notification"):
        return
    cwd = data.get("cwd") or os.getcwd()
    event = {"kind": kind, "time": time.time(),
             "project": Path(cwd).name or cwd, "cwd": cwd,
             "session": data.get("session_id", ""), "pids": ancestor_pids(),
             "account": account_name()}
    if kind == "Notification":
        event["message"] = data.get("message", "")
        event["type"] = data.get("notification_type", "")
    else:
        event["snippet"], event["took"] = last_turn(
            read_tail(data.get("transcript_path", "")))
    try:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
