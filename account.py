"""Linking the pet to the user's own Claude Code login.

The pet never stores an API key or token. Every request goes through the
`claude` CLI, so it runs on (and bills) whatever account Claude Code is
signed in to on this computer. This module only checks that link and
opens a terminal for installing Claude Code or signing in.
"""

import json
import shutil
import subprocess
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
INSTALL_COMMAND = "irm https://claude.ai/install.ps1 | iex"
# Phrases in a failed reply that mean the login is missing or expired.
AUTH_ERRORS = ("not logged in", "please run /login", "invalid api key",
               "authentication_error", "oauth token has expired", "/login")


def find_claude() -> str | None:
    """Path of the Claude Code CLI, or None when it is not installed."""
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude.exe"
    return str(local) if local.exists() else None


def status() -> dict:
    """Check whether Claude Code is installed and signed in.

    Returns:
        {"installed": bool, "linked": bool, "email": str, "plan": str,
         "method": str, "claude": path or None}
    """
    claude = find_claude()
    info = {"installed": bool(claude), "linked": False, "email": "",
            "plan": "", "method": "", "claude": claude}
    if not claude:
        return info
    try:
        out = subprocess.run([claude, "auth", "status"], capture_output=True,
                             text=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return info
    info.update(linked=bool(data.get("loggedIn")), email=data.get("email") or "",
                plan=data.get("subscriptionType") or "",
                method=data.get("authMethod") or "")
    return info


def describe(info: dict) -> str:
    """Short account line for the menu, e.g. "you@mail.com (pro)"."""
    if not info.get("linked"):
        return "not linked"
    who = info.get("email") or ("API key" if "api" in info.get("method", "") else "signed in")
    plan = info.get("plan")
    return f"{who} ({plan})" if plan else who


def is_auth_error(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in AUTH_ERRORS)


def open_terminal(command: str) -> None:
    """Run a command in a new visible PowerShell window (it stays open)."""
    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    subprocess.Popen([shell, "-NoExit", "-Command", command],
                     creationflags=CREATE_NEW_CONSOLE)


def install() -> None:
    """Install Claude Code with Anthropic's official installer."""
    open_terminal(INSTALL_COMMAND)


def login(claude: str | None, console: bool = False) -> None:
    """Start Claude Code's browser sign-in.

    Args:
        claude: path of the CLI (found again when None).
        console: sign in to the Anthropic Console (API credits) instead of
            a Claude Pro/Max subscription.
    """
    exe = claude or find_claude() or "claude"
    flag = "--console" if console else "--claudeai"
    open_terminal(f"& '{exe}' auth login {flag}")


def logout(claude: str | None) -> None:
    exe = claude or find_claude() or "claude"
    subprocess.run([exe, "auth", "logout"], capture_output=True, timeout=30,
                   creationflags=CREATE_NO_WINDOW)
