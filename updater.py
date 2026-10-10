"""Updates for installed copies of the pet (pip or uv tool installs).

Once the user has linked an AI provider, the pet checks PyPI for a newer
boba-pet release (one small request, nothing about the user is sent). If
one exists, it asks "Update now / Later"; "Later" asks again the next day.

Updating runs in a separate PowerShell window-less process: it waits for
the pet to exit (the running boba-pet.exe would otherwise be locked),
upgrades with the same tool it was installed with, and starts the pet again.

A source checkout (git clone) has no installed version and is never updated
here; use git pull.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PACKAGE = "boba-pet"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
RELEASE_URL = "https://api.github.com/repos/AbdulRahmanAzam/boba-pet/releases/tags/v{}"
RELEASES_PAGE = "https://github.com/AbdulRahmanAzam/boba-pet/releases"
CHECK_EVERY_SECONDS = 6 * 3600
FIRST_CHECK_DELAY_MS = 60_000
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def installed_version() -> str | None:
    """The installed boba-pet version, or None when run from source."""
    from importlib.metadata import PackageNotFoundError, version

    if not Path(__file__).resolve().parent.name == "boba_pet":
        return None  # a clone: its files are not the installed package
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return None


def parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def newer(latest: str, current: str) -> bool:
    return parse(latest) > parse(current)


def latest_version(timeout: float = 10) -> str:
    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": PACKAGE})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["info"]["version"]


def release_notes(ver: str, lines: int = 6, timeout: float = 10) -> str:
    """The first lines of the GitHub release notes for ver, or ""."""
    try:
        req = urllib.request.Request(RELEASE_URL.format(ver),
                                     headers={"User-Agent": PACKAGE,
                                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r).get("body") or ""
    except (OSError, ValueError):
        return ""
    keep = [ln.rstrip() for ln in body.replace("\r\n", "\n").split("\n") if ln.strip()]
    return "\n".join(keep[:lines])


def upgrade_command() -> list[str]:
    """Upgrade with the tool that installed the pet."""
    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").exists():  # uv tool install (no pip inside)
        uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")
        return [uv, "tool", "upgrade", PACKAGE]
    python = Path(sys.executable).with_name("python.exe")
    return [str(python if python.exists() else sys.executable),
            "-m", "pip", "install", "--upgrade", PACKAGE]


def relaunch_command() -> list[str]:
    exe = shutil.which("boba-pet")
    if exe:
        return [exe]
    python = Path(sys.executable).with_name("pythonw.exe")
    return [str(python if python.exists() else sys.executable), "-m", "boba_pet"]


def _ps_list(items: list[str]) -> str:
    return "@(" + ",".join("'" + s.replace("'", "''") + "'" for s in items) + ")"


def start_update(log: Path) -> None:
    """Start the helper that waits for this pet to exit, upgrades, and starts
    it again. The caller quits the pet right after this returns."""
    pids = [os.getpid()]
    parent = os.getppid()
    if parent:
        pids.append(parent)  # boba-pet.exe launcher, if that's what started us
    up, again = upgrade_command(), relaunch_command()
    script = (
        f"$ErrorActionPreference='Continue';"
        f"foreach($i in {_ps_list([str(p) for p in pids])}){{"
        f"$p=Get-Process -Id ([int]$i) -ErrorAction SilentlyContinue;"
        f"if($p -and $p.ProcessName -match 'python|boba-pet'){{$p.WaitForExit(30000)|Out-Null}}}};"
        f"Start-Sleep -Milliseconds 500;"
        f"$u={_ps_list(up)};& $u[0] $u[1..($u.Count-1)] *> '{str(log).replace(chr(39), chr(39) * 2)}';"
        f"$r={_ps_list(again)};"
        f"if($r.Count -gt 1){{Start-Process -FilePath $r[0] -ArgumentList $r[1..($r.Count-1)]}}"
        f"else{{Start-Process -FilePath $r[0]}}")
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
    # Not DETACHED_PROCESS: PowerShell exits at once without a console.
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):  # outlive a launcher's job object
        try:
            subprocess.Popen(args, creationflags=flags | extra, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
            return
        except OSError:
            if not extra:
                raise


def today() -> str:
    return time.strftime("%Y-%m-%d")
