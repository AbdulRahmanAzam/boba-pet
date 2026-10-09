"""Claude Code status line that also feeds the pet's usage meter.

Claude Code runs this after each update with a JSON description of the
session on stdin, including the plan's rate limits (5-hour and weekly use).
It saves those limits for the pet (see usage.py) and prints one short line:
model, context use and the limits. Standard library only; never fails loudly.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usage  # noqa: E402


def account_name() -> str:
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or ".claude").name
    return folder.removeprefix(".claude").lstrip("-_")


def wrapped_command() -> str:
    """The status line this account had before the pet's, if any."""
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        saved = json.loads(usage.WRAPPED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return saved.get(str(folder.resolve()).lower(), "")


def main() -> None:
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        data = {}
    limits = data.get("rate_limits") or {}
    if limits:
        usage.record_claude_limits(account_name(), limits)
    previous = wrapped_command()
    if previous:  # keep the account's own status line, exactly as it was
        out = subprocess.run(previous, input=raw, capture_output=True, shell=True,
                             timeout=10, creationflags=0x08000000)
        sys.stdout.buffer.write(out.stdout)
        return
    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(model)
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"context {float(ctx):.0f}%")
    for key, label in usage.WINDOWS.items():
        win = limits.get(key) or {}
        if win.get("used_percentage") is not None:
            parts.append(f"{label} {float(win['used_percentage']):.0f}%")
    sys.stdout.buffer.write(" · ".join(parts).encode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a status line must never break Claude Code
        pass
