"""Watches running Claude Code sessions through their transcripts.

Every Claude Code account keeps one transcript per session at
~/.claude*/projects/<project>/<session>.jsonl. Watcher.poll() reads only the
lines added since the last poll, for transcripts changed in the last few
minutes, and works out per session whether a turn is running. It also
reports notable moments: a turn finished, a test run failed or passed, an
API error (like a usage limit). Message text never leaves this machine and
is not kept; only short command names and error lines are. The one
exception is opt-in: for folders listed in Watcher.prompt_dirs (hackathon
mode's prompt coach), each new prompt you type is passed on as a "prompt"
event.

The pet's own `claude -p` calls (entrypoint "sdk-cli") are skipped.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

FULL_SCAN_SECONDS = 60  # list every transcript at most this often
RECENT_SECONDS = 15 * 60  # only transcripts written this recently are read
STALL_SECONDS = 10 * 60  # a "running" turn this quiet has died or is stuck
FIRST_READ_BYTES = 256 * 1024  # catching up on an already-open transcript
TEST_CMD = re.compile(
    r"\b(pytest|jest|vitest|mocha|unittest|phpunit|rspec|"
    r"(npm|pnpm|yarn|bun)( run)? test|npx (hardhat|jest|vitest) test|hardhat test|"
    r"cargo test|go test|dotnet test|gradle\w* test|mvn\w* test|ctest)\b")
TEST_FAILED = re.compile(
    r"\b[1-9]\d* (failed|failing|errors?)\b|^(FAILED|FAIL)\b|\bTests?:.*\b[1-9]\d* failed",
    re.MULTILINE)
TEST_PASSED = re.compile(r"\b[1-9]\d* (passed|passing)\b|\bok\b.*\btest", re.IGNORECASE)
INTERRUPTED = "[Request interrupted by user"


def config_dirs() -> list[Path]:
    """Every Claude Code account folder (~/.claude, ~/.claude-<name>, ...)."""
    home = Path.home()
    try:
        return [d for d in home.iterdir()
                if d.name.startswith(".claude") and (d / "projects").is_dir()]
    except OSError:
        return []


def account_of(config_dir: Path) -> str:
    """~/.claude-work gives "work"; plain ~/.claude gives ""."""
    return config_dir.name.removeprefix(".claude").lstrip("-_")


@dataclass
class Session:
    """What the pet knows about one Claude Code session."""

    id: str
    account: str
    project: str = ""
    cwd: str = ""
    running: bool = False
    started: float = 0.0  # when the running turn began
    last_write: float = 0.0  # newest transcript line (time this poll saw it)
    interactive: bool | None = None  # None until an entry says which
    tests: dict[str, str] = field(default_factory=dict)  # tool_use id: command

    @property
    def active(self) -> bool:
        return self.running and time.time() - self.last_write < STALL_SECONDS


@dataclass
class Event:
    """Something worth reacting to. kind: finish, interrupted, tests_failed,
    tests_passed, api_error, tool_error, prompt (detail = the prompt text)."""

    kind: str
    session: Session
    detail: str = ""
    took: float = 0.0


def _text_of(content: object) -> str:
    """Plain text of a tool result's content (a string or text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _under(cwd: str, folder: str) -> bool:
    if not cwd or not folder:
        return False
    a = str(Path(cwd)).lower().rstrip("\\/")
    b = str(Path(folder)).lower().rstrip("\\/")
    return a == b or a.startswith(b + "\\") or a.startswith(b + "/")


class Watcher:
    """Incremental reader for all recent Claude Code transcripts."""

    def __init__(self) -> None:
        self.files: dict[Path, int] = {}  # transcript: bytes already read
        self.sessions: dict[str, Session] = {}
        self.primed = False  # the first poll only learns state, no events
        self.prompt_dirs: list[str] = []  # folders whose prompts become events
        # Cheap discovery: a project folder's mtime changes when a transcript
        # is created in it, so only changed folders are listed; known recent
        # transcripts are just stat()ed. A full listing runs every FULL_SCAN.
        self.dir_mtimes: dict[Path, float] = {}
        self.last_full = 0.0

    def poll(self) -> list[Event]:
        """Read new transcript lines. Returns events since the last poll."""
        now = time.time()
        events: list[Event] = []
        full = now - self.last_full > FULL_SCAN_SECONDS
        if full:
            self.last_full = now
        for cfg in config_dirs():
            account = account_of(cfg)
            try:
                folders = list(os.scandir(cfg / "projects"))
            except OSError:
                continue
            candidates: list[Path] = []
            for entry in folders:
                if not entry.is_dir():
                    continue
                folder = Path(entry.path)
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if full or self.dir_mtimes.get(folder) != mtime:
                    self.dir_mtimes[folder] = mtime
                    candidates += folder.glob("*.jsonl")
            candidates += [p for p in self.files if p.parent.parent == cfg / "projects"]
            for path in dict.fromkeys(candidates):
                try:
                    st = path.stat()
                except OSError:
                    continue
                if now - st.st_mtime > RECENT_SECONDS and path not in self.files:
                    continue
                events += self._read(path, st.st_size, account)
        # forget sessions that went quiet long ago
        for sid, s in list(self.sessions.items()):
            if now - s.last_write > RECENT_SECONDS + 60 and not s.running:
                self.sessions.pop(sid, None)
        for path in [p for p in self.files if p.stem not in self.sessions]:
            try:
                if now - path.stat().st_mtime > RECENT_SECONDS:
                    self.files.pop(path, None)
            except OSError:
                self.files.pop(path, None)
        first, self.primed = not self.primed, True
        return [] if first else events

    def running(self) -> list[Session]:
        """Interactive sessions with a turn in progress right now."""
        return [s for s in self.sessions.values() if s.active and s.interactive]

    def _read(self, path: Path, size: int, account: str) -> list[Event]:
        offset = self.files.get(path)
        if offset is None:  # new to us: read just the end to learn its state
            offset = max(0, size - FIRST_READ_BYTES)
        if size < offset:  # rewritten
            offset = 0
        if size == offset:
            self.files[path] = offset
            return []
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
        except OSError:
            return []
        end = chunk.rfind(b"\n") + 1  # leave a half-written line for later
        skip_first = offset > 0 and path not in self.files  # landed mid-line
        self.files[path] = offset + end
        session = self.sessions.get(path.stem)
        if session is None:
            session = self.sessions[path.stem] = Session(
                path.stem, account, project=path.parent.name)
        events: list[Event] = []
        lines = chunk[:end].decode("utf-8", errors="replace").splitlines()
        for line in lines[1 if skip_first else 0:]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            events += self._entry(session, entry)
        if lines:
            session.last_write = time.time()
        return events if session.interactive else []

    def _entry(self, s: Session, e: dict) -> list[Event]:
        kind = e.get("type")
        if e.get("entrypoint"):
            s.interactive = not str(e["entrypoint"]).startswith("sdk")
        if e.get("cwd"):
            s.cwd = e["cwd"]
            s.project = Path(e["cwd"]).name or e["cwd"]
        msg = e.get("message") or {}
        content = msg.get("content")
        now = time.time()
        if kind == "user":
            if isinstance(content, str) or (isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") in ("text", "image")
                    for b in content)):
                text = _text_of(content)
                if text.startswith(INTERRUPTED):
                    return self._end(s, "interrupted")
                if e.get("isMeta") or text.lstrip().startswith(("<command-", "<local-command")):
                    return []
                s.running, s.started, s.tests = True, now, {}
                if self.prompt_dirs and any(_under(s.cwd, d) for d in self.prompt_dirs):
                    return [Event("prompt", s, text)]
                return []
            if isinstance(content, list):  # tool results: the turn goes on
                s.running = True
                return self._results(s, content)
            return []
        if kind == "assistant":
            if e.get("isApiErrorMessage"):
                status = e.get("apiErrorStatus") or ""
                limits = e.get("quotaLimits") or {}
                detail = ("usage limit reached" if status == 429 or limits.get(
                    "status") == "rejected" else f"API error {status}".strip())
                return [Event("api_error", s, detail)] + self._end(s, "finish")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_use":
                    s.running = True
                    cmd = str((block.get("input") or {}).get("command", ""))
                    if block.get("name") in ("Bash", "PowerShell") and TEST_CMD.search(cmd):
                        s.tests[block.get("id", "")] = cmd
            if msg.get("stop_reason") in ("end_turn", "stop_sequence"):
                return self._end(s, "finish")
            return []
        if kind == "system" and e.get("subtype") in ("turn_duration", "stop_hook_summary"):
            return self._end(s, "finish")
        return []

    def _results(self, s: Session, content: list) -> list[Event]:
        events: list[Event] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            out = _text_of(block.get("content"))
            cmd = s.tests.pop(block.get("tool_use_id", ""), None)
            if cmd is not None:
                if block.get("is_error") or TEST_FAILED.search(out):
                    line = next((ln.strip() for ln in reversed(out.splitlines())
                                 if TEST_FAILED.search(ln)), "")  # the summary
                    events.append(Event("tests_failed", s, line[:120] or cmd[:80]))
                elif TEST_PASSED.search(out):
                    events.append(Event("tests_passed", s, cmd[:80]))
            elif block.get("is_error") and "user doesn't want" not in out:
                events.append(Event("tool_error", s))
        return events

    def _end(self, s: Session, kind: str) -> list[Event]:
        if not s.running:
            return []
        s.running = False
        return [Event(kind, s, took=time.time() - s.started if s.started else 0.0)]
