"""Usage meter: how much of each AI provider's allowance is used.

All sources are local files; nothing is sent anywhere.

- Claude Code: pet_status.py runs as Claude Code's status line and saves the
  plan's 5-hour and weekly limits (which Claude Code passes to status lines)
  into USAGE_FILE. Today's tokens are summed from the transcripts in
  ~/.claude*/projects.
- Codex: its session logs in ~/.codex/sessions carry the ChatGPT plan's
  5-hour and weekly limits and token counts.
- Groq, Gemini API, Antigravity (only the pet calls these): providers.py
  calls record() after each request, with Groq's daily request limit taken
  from its response headers.
"""

import json
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

USAGE_FILE = Path.home() / ".claude-pet-usage.json"
WARN_AT = (80, 95)  # percent of a limit that triggers a warning
STALE_LIMITS = 6 * 3600  # Claude limits older than this are not trusted
CODEX_DIR = Path.home() / ".codex" / "sessions"
# status lines that were there before ours: pet_status.py runs and prints them
WRAPPED_FILE = Path.home() / ".claude-pet-statusline.json"
APP_DIR = Path(__file__).resolve().parent
WINDOWS = {"five_hour": "5h", "seven_day": "week"}


# ---------- the shared usage file ----------
def load() -> dict:
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    tmp = USAGE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, USAGE_FILE)
    except OSError:
        pass


def record(provider: str, tokens: int = 0, requests: int = 1,
           limit: int | None = None, remaining: int | None = None) -> None:
    """Count one of the pet's own requests (Groq, Gemini API, Antigravity).

    limit / remaining: the provider's daily request allowance, when known.
    """
    data = load()
    today = date.today().isoformat()
    day = data.setdefault("pet", {}).get(provider, {})
    if day.get("day") != today:
        day = {"day": today, "tokens": 0, "requests": 0}
    day["tokens"] += int(tokens or 0)
    day["requests"] += requests
    if limit:
        day["limit"], day["remaining"] = int(limit), int(remaining or 0)
    data["pet"][provider] = day
    save(data)


def record_claude_limits(account: str, rate_limits: dict) -> None:
    """Called by the status line with Claude Code's rate_limits."""
    data = load()
    limits = {k: {"pct": float(v.get("used_percentage") or 0),
                  "resets_at": v.get("resets_at")}
              for k, v in (rate_limits or {}).items() if k in WINDOWS and v}
    if not limits:
        return
    data.setdefault("claude", {})[account] = {"time": time.time(), **limits}
    save(data)


# ---------- Claude Code status line ----------
def status_line_command() -> str:
    """The command Claude Code runs for the status line (our pet_status.py)."""
    py = Path(sys.executable)
    console = py.with_name("python.exe")  # status lines need a stdout
    exe = console if console.exists() else py
    return f'"{exe}" "{APP_DIR / "pet_status.py"}"'


def status_line_targets() -> list[Path]:
    """settings.json of every Claude Code account folder."""
    home = Path.home()
    return [d / "settings.json" for d in home.glob(".claude*")
            if (d / "projects").is_dir() and not d.name.endswith("backups")]


def install_status_line() -> tuple[int, int]:
    """Make pet_status.py the status line of every Claude Code account.

    An account that already has a status line keeps it: the old command is
    saved in WRAPPED_FILE and pet_status.py runs it and prints its output
    unchanged. Returns (installed, skipped). Each changed settings file is
    backed up next to itself first.
    """
    done = skipped = 0
    try:
        wrapped = json.loads(WRAPPED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        wrapped = {}
    for path in status_line_targets():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            skipped += 1
            continue
        current = (data.get("statusLine") or {}).get("command", "")
        if "pet_status.py" in current:
            done += 1
            continue
        if current:
            wrapped[str(path.parent.resolve()).lower()] = current
            WRAPPED_FILE.write_text(json.dumps(wrapped, indent=1), encoding="utf-8")
        if path.exists():
            shutil.copy(path, path.with_suffix(".json.pet-backup"))
        data["statusLine"] = {"type": "command", "command": status_line_command(),
                              "padding": 0}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        done += 1
    return done, skipped


def status_line_installed() -> bool:
    for path in status_line_targets():
        try:
            if "pet_status.py" in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


# ---------- Claude Code: today's tokens from transcripts ----------
class ClaudeTokens:
    """Sums today's tokens across all Claude Code accounts, incrementally."""

    def __init__(self) -> None:
        self.day = ""
        self.offsets: dict[Path, int] = {}
        self.seen: set[str] = set()
        self.total = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

    def scan(self) -> dict:
        today = date.today()
        if self.day != today.isoformat():
            self.__init__()
            self.day = today.isoformat()
        midnight = datetime.combine(today, datetime.min.time()).timestamp()
        for cfg in Path.home().glob(".claude*"):
            projects = cfg / "projects"
            if not projects.is_dir() or cfg.name.endswith("backups"):
                continue
            for path in projects.glob("**/*.jsonl"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_mtime < midnight or self.offsets.get(path) == st.st_size:
                    continue
                self._read(path, st.st_size, midnight)
        return dict(self.total)

    def _read(self, path: Path, size: int, midnight: float) -> None:
        start = self.offsets.get(path, 0)
        if size < start:
            start = 0
        try:
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(size - start)
        except OSError:
            return
        end = chunk.rfind(b"\n") + 1
        self.offsets[path] = start + end
        for raw in chunk[:end].split(b"\n"):
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            msg = e.get("message") or {}
            usage = msg.get("usage")
            if e.get("type") != "assistant" or not usage:
                continue
            key = f"{msg.get('id')}:{e.get('requestId')}"  # one reply spans entries
            if key in self.seen:
                continue
            try:
                ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts.timestamp() < midnight:
                continue
            self.seen.add(key)
            self.total["input"] += usage.get("input_tokens") or 0
            self.total["output"] += usage.get("output_tokens") or 0
            self.total["cache_write"] += usage.get("cache_creation_input_tokens") or 0
            self.total["cache_read"] += usage.get("cache_read_input_tokens") or 0


# ---------- Codex ----------
def _tail_events(path: Path, nbytes: int = 512 * 1024) -> list[dict]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines()[1 if size > nbytes else 0:]:
        if '"token_count"' in line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def codex_usage() -> dict | None:
    """Codex plan limits (newest log) and today's tokens, or None."""
    if not CODEX_DIR.is_dir():
        return None
    files = []
    for back in range(8):  # logs live in sessions/YYYY/MM/DD/
        day = date.today() - timedelta(days=back)
        folder = CODEX_DIR / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        if folder.is_dir():
            files += list(folder.glob("*.jsonl"))
    if not files:
        return None
    files.sort(key=lambda f: f.stat().st_mtime)
    today_dir = CODEX_DIR / f"{date.today():%Y/%m/%d}"
    tokens, limits, at = 0, {}, 0.0
    for path in files:
        events = _tail_events(path)
        if path.parent == today_dir:
            totals = [((e.get("payload") or {}).get("info") or {}).get(
                "total_token_usage", {}).get("total_tokens", 0) for e in events]
            tokens += max(totals or [0])
        for e in reversed(events):
            rl = (e.get("payload") or {}).get("rate_limits")
            if rl:
                if path.stat().st_mtime >= at:
                    at = path.stat().st_mtime
                    limits = rl
                break
    out: dict = {"tokens": tokens, "time": at}
    for name, key in (("primary", "five_hour"), ("secondary", "seven_day")):
        win = limits.get(name) or {}
        if win:
            minutes = win.get("window_minutes") or 0
            out[key if minutes in (300, 10080) or not minutes else
                ("five_hour" if minutes < 1440 else "seven_day")] = {
                "pct": float(win.get("used_percent") or 0),
                "resets_at": win.get("resets_at")}
    out["plan"] = limits.get("plan_type") or ""
    return out


# ---------- one summary for the pet ----------
def fresh_window(win: dict | None, now: float) -> dict | None:
    """A limit window, or zero use once its reset time has passed."""
    if not win:
        return None
    resets = win.get("resets_at")
    if resets and float(resets) < now:
        return {"pct": 0.0, "resets_at": None}
    return win


def snapshot(claude_tokens: ClaudeTokens | None = None) -> dict:
    """{provider: {"pct": worst limit % or None, "windows": {...}, ...}}."""
    now = time.time()
    data = load()
    out: dict = {}
    accounts = {}
    for account, rec in (data.get("claude") or {}).items():
        if now - rec.get("time", 0) > STALE_LIMITS:
            continue
        wins = {k: w for k in WINDOWS if (w := fresh_window(rec.get(k), now))}
        if wins:
            accounts[account] = {"windows": wins, "time": rec.get("time", 0)}
    claude: dict = {"accounts": accounts}
    if accounts:  # the meter shows the account used most recently
        latest = max(accounts.values(), key=lambda a: a["time"])
        claude["windows"] = latest["windows"]
        claude["pct"] = max(w["pct"] for w in latest["windows"].values())
    if claude_tokens is not None:
        claude["tokens"] = claude_tokens.scan()
    out["claude"] = claude
    codex = codex_usage()
    if codex:
        wins = {k: w for k in WINDOWS if (w := fresh_window(codex.get(k), now))}
        out["codex"] = {"windows": wins, "tokens": codex["tokens"],
                        "plan": codex.get("plan", "")}
        if wins:
            out["codex"]["pct"] = max(w["pct"] for w in wins.values())
    today = date.today().isoformat()
    for provider, day in (data.get("pet") or {}).items():
        if day.get("day") != today:
            continue
        entry = {"tokens": day.get("tokens", 0), "requests": day.get("requests", 0)}
        if day.get("limit"):
            used = day["limit"] - day.get("remaining", 0)
            entry["pct"] = 100.0 * used / day["limit"]
            entry["limit"] = day["limit"]
        out[provider] = entry
    return out


def short_tokens(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(int(n))


def reset_text(resets_at: object) -> str:
    try:
        t = datetime.fromtimestamp(float(resets_at))  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return ""
    if t.date() == date.today():
        return t.strftime("%I:%M %p").lstrip("0").lower()
    return t.strftime("%a %I:%M %p").replace(" 0", " ").lower()


def ring_text(entry: dict) -> tuple[float, str]:
    """(progress 0..1, pill text) for the usage ring."""
    wins = entry.get("windows") or {}
    if wins:  # the fuller window is the one that will stop you
        key = max(wins, key=lambda k: wins[k]["pct"])
        pct = wins[key]["pct"]
        return pct / 100, f"{WINDOWS[key]} {pct:.0f}%"
    if entry.get("pct") is not None:
        return entry["pct"] / 100, f"day {entry['pct']:.0f}%"
    tokens = entry.get("tokens")
    if isinstance(tokens, dict):
        tokens = tokens.get("input", 0) + tokens.get("output", 0) + tokens.get(
            "cache_write", 0)
    if tokens:
        return 0.0, short_tokens(tokens)
    if entry.get("requests"):
        return 0.0, f"{entry['requests']} req"
    return 0.0, "—"


def describe(provider: str, name: str, entry: dict) -> str:
    """Markdown lines for the usage panel."""
    lines = [f"**{name}**" + (f" ({entry['plan']})" if entry.get("plan") else "")]
    for key, win in (entry.get("windows") or {}).items():
        when = reset_text(win.get("resets_at"))
        lines.append(f"- {WINDOWS[key]} limit: **{win['pct']:.0f}%** used"
                     + (f", resets {when}" if when else ""))
    if provider == "claude" and len(entry.get("accounts") or {}) > 1:
        for account, rec in entry["accounts"].items():
            bits = " · ".join(f"{WINDOWS[k]} {w['pct']:.0f}%" for k, w in rec["windows"].items())
            lines.append(f"- {account or 'main'} account: {bits}")
    tokens = entry.get("tokens")
    if isinstance(tokens, dict):
        new = tokens["input"] + tokens["output"] + tokens["cache_write"]
        if new or tokens["cache_read"]:
            lines.append(f"- today: {short_tokens(new)} tokens "
                         f"({short_tokens(tokens['output'])} written, "
                         f"{short_tokens(tokens['cache_read'])} cache reads)")
    elif tokens:
        lines.append(f"- today: {short_tokens(tokens)} tokens")
    if entry.get("requests"):
        req = f"- today: {entry['requests']} requests"
        if entry.get("limit"):
            req += f" of {entry['limit']:,} a day ({entry['pct']:.0f}%)"
        lines.append(req)
    if len(lines) == 1:
        lines.append("- nothing used today")
    return "\n".join(lines)
