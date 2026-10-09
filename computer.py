"""Computer tools: lets the pet's AI operate this Windows PC like an assistant.

One toolset for every provider. Claude Code and Codex reach it through the
MCP server in pet_mcp.py (which forwards calls to the running pet); Groq and
the Gemini API call it directly through function calling.

Each call goes through Computer.call(), which checks the risk of the action:
"safe" actions (open an app, change volume, list windows, read a file...)
run at once; "ask" actions (typing, clicking, shell commands, closing
windows, seeing the screen, power...) first ask the user through the
`confirm` callback the pet provides. Mode "trust" skips the
questions and "off" disables every tool.

Whatever the mode, nothing gets deleted: blocked() refuses delete commands,
overwriting files, the Delete key, typing delete commands into terminals and
clicking buttons named Delete / Remove / Trash. It is a best-effort pattern
check, not a sandbox.
"""

import base64
import ctypes
import difflib
import html
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

import pet_paths

CREATE_NO_WINDOW = 0x08000000
OUTPUT_LIMIT = 6000  # characters of tool output handed back to the model
EXEC_EXTS = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".com", ".scr", ".js", ".jse"}
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "AppData",
             "$Recycle.Bin", ".cache", "site-packages", "dist", "build", ".next"}
SHOTS_DIR = pet_paths.SHOTS_DIR
SHOT_MAX_W = 1400  # screenshots are shrunk to about this width for the model
SCRIPT_EXTS = {".bat", ".cmd", ".ps1", ".vbs", ".js", ".jse", ".py"}
FOLDERS = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads",
           "pictures": "Pictures", "music": "Music", "videos": "Videos", "home": ""}

PROMPT = (
    "Your PC tools (named mcp__pc__*) are available now, even if earlier in this "
    "chat you had no tools or PowerShell was disabled; use them, never say you "
    "can't reach the PC. "
    "You can operate the user's Windows PC with the pet's computer tools: open "
    "apps, files, folders and websites; manage windows; type and press keys; "
    "click; see or read the screen; media and volume; system info, processes "
    "and power; clipboard; files; commands; web pages; reminders and to-dos. "
    "Before your first tool call, say in one short sentence what you are about "
    "to do (it is read aloud), e.g. 'Okay, opening WhatsApp and Mufis's chat.' "
    "Do what the user asks, then confirm in one short sentence what you did. "
    "Prefer `open` and "
    "`windows` over commands. After opening an app, `wait` 2-4 s for it to load. "
    "Focus the right window before typing or pressing keys. To click something, "
    "take a screenshot first and click its coordinates; check with another "
    "screenshot when it matters. Never delete anything: deleting files, "
    "messages, chats, emails or data is blocked, so don't try and don't look "
    "for workarounds; tell the user it's not allowed. If an action is declined "
    "or blocked, don't retry it.\n"
    "WhatsApp: open 'whatsapp', wait, focus it, press ctrl+f, type the contact "
    "name, wait 1 s, press down then enter (or click the chat in a "
    "screenshot). The message box is then focused: type the text, press enter "
    "to send. To read a chat use screen read_text or a screenshot. If `open` "
    "says it opened WhatsApp Web in the browser instead: wait 6 s, take a "
    "screenshot, click the 'Search' box at the top left, type the name, wait "
    "2 s, screenshot and click the chat, then click the 'Type a message' box, "
    "type the text and press enter. If it shows a QR code, tell the user to "
    "scan it with their phone first.")

_obj = {"type": "object"}


def _spec(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": desc,
            "parameters": {**_obj, "properties": props, "required": required}}


SPECS = [
    _spec("open", "Open an installed app (by name, e.g. 'spotify', 'whatsapp', "
          "'calculator'), a file or folder path, a known folder ('downloads'), a "
          "website or any URI (https://, mailto:, ms-settings:, spotify:).",
          {"target": {"type": "string"}}, ["target"]),
    _spec("windows", "List open windows, or focus / minimize / maximize / restore / "
          "close one matched by part of its title or app name.",
          {"action": {"type": "string",
                      "enum": ["list", "focus", "minimize", "maximize", "restore", "close"]},
           "title": {"type": "string", "description": "part of the title or app name"}},
          ["action"]),
    _spec("keyboard", "Type text into the focused window and/or press a key combo "
          "like 'ctrl+s', 'enter', 'alt+tab', 'win+d', 'ctrl+shift+esc'. Focus the "
          "right window first.",
          {"text": {"type": "string"}, "keys": {"type": "string"}}, []),
    _spec("mouse", "Mouse at pixel x, y of the latest screenshot.",
          {"action": {"type": "string",
                      "enum": ["click", "double_click", "right_click", "move", "scroll"]},
           "x": {"type": "integer"}, "y": {"type": "integer"},
           "amount": {"type": "integer", "description": "scroll notches, + up / - down"}},
          ["action"]),
    _spec("screen", "screenshot: capture the main screen (an image, for models that "
          "can see). read_text: the text and controls of a window (or the focused "
          "one), for any model.",
          {"action": {"type": "string", "enum": ["screenshot", "read_text"]},
           "title": {"type": "string"}}, ["action"]),
    _spec("media", "Media keys and volume.",
          {"action": {"type": "string",
                      "enum": ["play_pause", "next", "previous", "mute", "volume_up",
                               "volume_down", "set_volume"]},
           "level": {"type": "integer", "description": "0-100 for set_volume"}},
          ["action"]),
    _spec("system", "info: time, battery, CPU, memory, disk, network. processes: top "
          "apps by memory. kill: end an app by name. brightness: 0-100. lock, sleep, "
          "shutdown, restart (in 60 s), cancel_shutdown.",
          {"action": {"type": "string",
                      "enum": ["info", "processes", "kill", "brightness", "lock",
                               "sleep", "shutdown", "restart", "cancel_shutdown"]},
           "name": {"type": "string"}, "level": {"type": "integer"}}, ["action"]),
    _spec("clipboard", "Read or set the clipboard text.",
          {"action": {"type": "string", "enum": ["get", "set"]},
           "text": {"type": "string"}}, ["action"]),
    _spec("files", "find: files by name (query, optional folder). list: a folder. "
          "read: a text file. write: create or overwrite a text file.",
          {"action": {"type": "string", "enum": ["find", "list", "read", "write"]},
           "path": {"type": "string"}, "query": {"type": "string"},
           "content": {"type": "string"}}, ["action"]),
    _spec("run_command", "Run a PowerShell command and get its output (60 s limit).",
          {"command": {"type": "string"}}, ["command"]),
    _spec("web_page", "Fetch a web page and return its readable text.",
          {"url": {"type": "string"}}, ["url"]),
    _spec("reminder", "remind: show and say a reminder after `minutes` or at `at` "
          "(HH:MM, 24h). task: add a to-do to the pet's task list.",
          {"action": {"type": "string", "enum": ["remind", "task"]},
           "text": {"type": "string"}, "minutes": {"type": "number"},
           "at": {"type": "string"}}, ["action", "text"]),
    _spec("wait", "Pause, e.g. for an app or page to load (max 15 s).",
          {"seconds": {"type": "number"}}, ["seconds"]),
]
NAMES = {s["name"] for s in SPECS}
# Set by the pet when the user interrupts: tool calls stop and typing halts.
CANCEL = threading.Event()
NEEDS_VISION = {"screen.screenshot"}


def risk(name: str, args: dict) -> str:
    """"safe" or "ask" for one call."""
    action = str(args.get("action", ""))
    if name == "open":
        target = str(args.get("target", "")).strip().strip('"')
        return "ask" if Path(target).suffix.lower() in EXEC_EXTS else "safe"
    if name == "windows":
        return "ask" if action == "close" else "safe"
    if name in ("keyboard", "mouse", "run_command"):
        return "ask" if not (name == "mouse" and action == "move") else "safe"
    if name == "screen":
        return "ask"  # the screen may show private things
    if name == "system":
        return "safe" if action in ("info", "processes", "brightness",
                                    "cancel_shutdown") else "ask"
    if name == "clipboard":
        return "ask" if action == "get" else "safe"
    if name == "files":
        return "ask" if action == "write" else "safe"
    return "safe"


# ---------- the no-delete guard ----------
DELETE_CMD = re.compile(
    r"(?ix)"
    r"\b(remove|uninstall|unregister|dismount)-\w+ | \bclear-(?!host\b)\w+ | \bformat-volume\b"
    r"|(^|[\s;|&(]) (rm|rmdir|rd|del|erase|ri|unlink|shred|srm|wipe) (\s|$)"
    r"|\b(diskpart|cipher\s+/w|vssadmin\s+delete|wevtutil\s+cl|sdelete)\b"
    r"|\bformat\s+[a-z]:"
    r"|\bgit\s+(clean|reset\s+--hard|push\s.*(-f\b|--force)|branch\s+-d|stash\s+(drop|clear))"
    r"|\b(os\.(remove|unlink|rmdir|removedirs)|shutil\.rmtree|rmSync|unlinkSync|rimraf)"
    r"|\.unlink\( | \.Delete\( | ::Delete\( | DeleteFile"
    r"|\bdrop\s+(table|database|schema)\b|\btruncate\s+table\b|\bdelete\s+from\b"
    r"|\$Recycle\.Bin|\bRecycleBin\b"
    r"|\breg\s+delete\b|\bschtasks\s+/delete\b|\bsc\s+delete\b|\bnet\s+user\s+\S+\s+/delete")
HIDDEN_CMD = re.compile(r"(?i)-e(nc(odedcommand)?)?\s+[A-Za-z0-9+/=]{16,}|FromBase64String"
                        r"|\biex\b|Invoke-Expression")
DELETE_KEYS = {"delete", "del"}
DELETE_UI = re.compile(
    r"(?i)\b(delete|remove|trash|erase|discard|uninstall|wipe|bin|"
    r"clear (chat|history|all|messages|data)|empty recycle bin)\b")
TERMINALS = {"windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe",
             "conhost.exe", "openconsole.exe", "wt.exe", "bash.exe", "mintty.exe",
             "wsl.exe", "alacritty.exe", "wezterm-gui.exe", "code.exe",
             "code - insiders.exe"}
NO_DELETE = "Blocked: this pet never deletes anything ({}). Tell the user it's not allowed."


def element_at(x: int, y: int) -> str:
    """Names of the UI element under a screen point and its two parents."""
    script = f"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase
$e = [System.Windows.Automation.AutomationElement]::FromPoint((New-Object System.Windows.Point {int(x)}, {int(y)}))
$w = [System.Windows.Automation.TreeWalker]::ControlViewWalker
for ($i = 0; $i -lt 3 -and $e; $i++) {{ $e.Current.Name; $e = $w.GetParent($e) }}
"""
    try:
        return powershell(script, 10)
    except subprocess.TimeoutExpired:
        return ""


def blocked(name: str, args: dict, point: tuple[int, int] | None = None) -> str | None:
    """Why this call would delete something, or None. Runs in every mode."""
    action = str(args.get("action", ""))
    if name == "run_command":
        cmd = str(args.get("command", ""))
        if DELETE_CMD.search(cmd):
            return NO_DELETE.format("delete command")
        if HIDDEN_CMD.search(cmd):
            return NO_DELETE.format("hidden or encoded command")
    if name == "files" and action == "write":
        path = Path(os.path.expanduser(str(args.get("path", ""))))
        if path.exists():
            return NO_DELETE.format("overwriting an existing file; use a new file name")
    if name == "open":
        target = Path(os.path.expandvars(os.path.expanduser(
            str(args.get("target", "")).strip().strip('"'))))
        if target.suffix.lower() in SCRIPT_EXTS and target.is_file():
            try:
                code = target.read_text(encoding="utf-8", errors="replace")[:500_000]
            except OSError:
                code = ""
            if DELETE_CMD.search(code):
                return NO_DELETE.format("script that deletes")
    if name == "keyboard":
        for chord in re.split(r"[\s,]+", str(args.get("keys", "")).lower()):
            parts = set(chord.split("+"))
            if parts & DELETE_KEYS or parts == {"ctrl", "d"}:  # ctrl+d deletes in Explorer
                return NO_DELETE.format("Delete key")
        text = str(args.get("text", ""))
        fg = find_window("") if text else None
        if fg and fg["app"].lower() in TERMINALS and (DELETE_CMD.search(text)
                                                      or HIDDEN_CMD.search(text)):
            return NO_DELETE.format("delete command typed into a terminal")
    if name == "mouse" and action in ("click", "double_click") and point:
        label = element_at(*point)
        if DELETE_UI.search(label):
            return NO_DELETE.format(f"button “{' '.join(label.split())[:40]}”")
    return None


def describe(name: str, args: dict) -> str:
    """Short human line for the approval popup and the thinking caption."""
    a = {k: v for k, v in args.items() if v not in (None, "")}
    action = a.get("action", "")

    def cut(text: object, n: int = 60) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= n else text[:n - 1] + "…"

    if name == "open":
        return f"Open {cut(a.get('target'))}"
    if name == "windows":
        return "List windows" if action == "list" else f"{action.title()} “{cut(a.get('title'), 40)}”"
    if name == "keyboard":
        bits = []
        if a.get("text"):
            bits.append(f"Type “{cut(a['text'])}”")
        if a.get("keys"):
            bits.append(f"Press {a['keys']}")
        return " then ".join(bits) or "Keyboard"
    if name == "mouse":
        where = f" at {a.get('x')}, {a.get('y')}" if "x" in a else ""
        return f"{action.replace('_', ' ').capitalize()}{where}"
    if name == "screen":
        return "See your screen" if action == "screenshot" else (
            f"Read the text in “{cut(a.get('title'), 40)}”" if a.get("title")
            else "Read the text of the focused window")
    if name == "media":
        return (f"Set volume to {a.get('level')}%" if action == "set_volume"
                else action.replace("_", " ").capitalize())
    if name == "system":
        return {"kill": f"Close the app {a.get('name')}", "info": "Check system info",
                "processes": "List running apps", "lock": "Lock the PC",
                "sleep": "Put the PC to sleep", "shutdown": "Shut down the PC in 60 s",
                "restart": "Restart the PC in 60 s", "cancel_shutdown": "Cancel shutdown",
                "brightness": f"Set brightness to {a.get('level')}%"}.get(action, action)
    if name == "clipboard":
        return "Read your clipboard" if action == "get" else "Copy text to the clipboard"
    if name == "files":
        return {"find": f"Find files “{cut(a.get('query'), 40)}”",
                "list": f"List {cut(a.get('path'), 50)}",
                "read": f"Read {cut(a.get('path'), 50)}",
                "write": f"Write {cut(a.get('path'), 50)}"}.get(action, "Files")
    if name == "run_command":
        return f"Run: {cut(a.get('command'), 80)}"
    if name == "web_page":
        return f"Read {cut(a.get('url'), 50)}"
    if name == "reminder":
        return f"Remind: {cut(a.get('text'), 50)}" if action == "remind" else (
            f"Add task: {cut(a.get('text'), 50)}")
    if name == "wait":
        return f"Wait {a.get('seconds')} s"
    return name


# ---------- Win32 helpers ----------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]


def _process_name(pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


def list_windows() -> list[dict]:
    """Visible app windows, front to back: {hwnd, title, app}."""
    me = os.getpid()
    out: list[dict] = []

    def visit(hwnd: int, _lp: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        if cloaked.value:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if title in ("Program Manager", "Windows Input Experience"):
            return True
        out.append({"hwnd": hwnd, "title": title, "app": _process_name(pid.value)})
        return True

    user32.EnumWindows(EnumWindowsProc(visit), 0)
    return out


def find_window(query: str) -> dict | None:
    q = (query or "").lower().strip()
    wins = list_windows()
    if not q:
        fg = user32.GetForegroundWindow()
        return next((w for w in wins if w["hwnd"] == fg), wins[0] if wins else None)
    for w in wins:  # front-most match wins
        if q in w["title"].lower() or q in w["app"].lower().removesuffix(".exe"):
            return w
    titles = {w["title"].lower(): w for w in wins}
    close = difflib.get_close_matches(q, list(titles), n=1, cutoff=0.5)
    return titles[close[0]] if close else None


def focus_window(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows only lets the foreground app hand focus over; a tapped Alt
    # key counts as user input and lifts that lock.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


# ---------- keyboard / mouse (SendInput) ----------
class _KeyInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyInput), ("mi", _MouseInput), ("pad", ctypes.c_byte * 32)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


def _send(inputs: list[_Input]) -> None:
    arr = (_Input * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(_Input))


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _Input:
    inp = _Input(type=1)
    inp.u.ki = _KeyInput(vk, scan, flags, 0, 0)
    return inp


VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
      "windows": 0x5B, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
      "escape": 0x1B, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
      "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "capslock": 0x14,
      "printscreen": 0x2C, "menu": 0x5D, "plus": 0xBB, "minus": 0xBD,
      "comma": 0xBC, "period": 0xBE, "volumeup": 0xAF, "volumedown": 0xAE,
      "mute": 0xAD, "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x5D}


def _vk_of(name: str) -> int:
    name = name.strip().lower()
    if name in VK:
        return VK[name]
    if len(name) == 1:
        res = user32.VkKeyScanW(ord(name))
        if res != -1:
            return res & 0xFF
    raise ValueError(f"Unknown key: {name}")


def press_keys(combo: str) -> None:
    """'ctrl+shift+esc', 'enter', 'alt+f4'; several combos split by spaces/commas."""
    for chord in re.split(r"[\s,]+", combo.strip()):
        if not chord:
            continue
        vks = [_vk_of(k) for k in chord.split("+") if k]
        ext = lambda v: 1 if v in EXTENDED else 0  # noqa: E731
        _send([_key(v, flags=ext(v)) for v in vks]
              + [_key(v, flags=2 | ext(v)) for v in reversed(vks)])
        time.sleep(0.05)


def type_text(text: str) -> None:
    for ch in text:
        if CANCEL.is_set():
            return
        if ch == "\n":
            press_keys("enter")
            continue
        code = ord(ch)
        units = ([code] if code < 0x10000 else
                 [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)])
        _send([_key(scan=u, flags=4) for u in units]
              + [_key(scan=u, flags=4 | 2) for u in units])
        time.sleep(0.004)


def mouse(action: str, x: int | None, y: int | None, amount: int = 0) -> None:
    if x is not None and y is not None:
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
    down_up = {"click": (0x2, 0x4), "double_click": (0x2, 0x4),
               "right_click": (0x8, 0x10)}
    if action in down_up:
        d, u = down_up[action]
        for _ in range(2 if action == "double_click" else 1):
            user32.mouse_event(d, 0, 0, 0, 0)
            user32.mouse_event(u, 0, 0, 0, 0)
            time.sleep(0.06)
    elif action == "scroll":
        user32.mouse_event(0x800, 0, 0, int(amount or -3) * 120, 0)


# ---------- apps ----------
_apps: dict = {"at": 0.0, "list": []}


def start_apps() -> list[tuple[str, str]]:
    """(name, AppID) of everything in the Start menu, cached for 10 minutes."""
    if time.time() - _apps["at"] > 600 or not _apps["list"]:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=CREATE_NO_WINDOW)
        apps = [tuple(line.split("\t", 1)) for line in out.stdout.splitlines()
                if "\t" in line]
        if apps:
            _apps.update(at=time.time(), list=apps)
    return _apps["list"]


APP_ALIASES = {"vs code": "visual studio code", "vscode": "visual studio code",
               "code": "visual studio code", "word": "microsoft word",
               "excel": "microsoft excel", "powerpoint": "microsoft powerpoint",
               "task manager": "task manager", "cmd": "command prompt",
               "terminal": "terminal", "file explorer": "file explorer",
               "explorer": "file explorer", "browser": "microsoft edge"}
WEB_APP_ID = re.compile(r"_crx_|\.(com|org|net|io)\b|https?:", re.I)
# Apps with a web version: opened in the browser when not installed.
WEB_FALLBACK = {"whatsapp": "https://web.whatsapp.com", "spotify": "https://open.spotify.com",
                "teams": "https://teams.microsoft.com", "outlook": "https://outlook.live.com",
                "telegram": "https://web.telegram.org", "discord": "https://discord.com/app",
                "gmail": "https://mail.google.com", "youtube": "https://www.youtube.com",
                "chatgpt": "https://chatgpt.com", "word": "https://www.office.com/launch/word",
                "excel": "https://www.office.com/launch/excel",
                "instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com",
                "linkedin": "https://www.linkedin.com", "github": "https://github.com"}


def find_app(name: str) -> tuple[str, str] | None:
    q = re.sub(r"\s+app$", "", name.lower().strip())
    apps = start_apps()
    q = APP_ALIASES.get(q, q)
    lower = [(n.lower(), n, a) for n, a in apps]
    # tiers: exact name, then whole-word match ("chrome" in "Google Chrome"),
    # then any substring; within a tier real apps beat web / extension
    # shortcuts, then the shortest name (the app itself, not "Chrome Remote Desktop")
    for test in (lambda n: n == q,
                 lambda n: re.search(rf"\b{re.escape(q)}\b", n) is not None,
                 lambda n: q in n):
        hits = [(n, a) for low, n, a in lower if test(low)]
        if hits:
            return min(hits, key=lambda h: (bool(WEB_APP_ID.search(h[1])), len(h[0])))
    close = difflib.get_close_matches(q, [low for low, _, _ in lower], n=1, cutoff=0.75)
    if close:
        return next((n, a) for low, n, a in lower if low == close[0])
    return None


def open_target(target: str) -> str:
    t = os.path.expandvars(os.path.expanduser(target.strip().strip('"')))
    low = t.lower()
    if low in FOLDERS:
        path = Path.home() / FOLDERS[low]
        os.startfile(path)
        return f"Opened {path}"
    if re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I) and not re.match(r"^[a-z]:[\\/]", t, re.I):
        os.startfile(t)  # https:, mailto:, ms-settings:, spotify: ...
        return f"Opened {t}"
    if re.fullmatch(r"(www\.)?[\w-]+(\.[\w-]+)+(/\S*)?", t) and not Path(t).exists():
        os.startfile("https://" + t)
        return f"Opened https://{t}"
    if Path(t).exists():
        os.startfile(t)
        return f"Opened {t}"
    app = find_app(t)
    web = WEB_FALLBACK.get(re.sub(r"^(my |the )|\s+(app|desktop|web)$", "", low).strip())
    if not app and web:
        os.startfile(web)
        return (f"'{target}' isn't installed as an app, so opened its web version "
                f"{web} in the browser (it may need a few seconds to load).")
    if app:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app[1]}"],
                         creationflags=CREATE_NO_WINDOW)
        return f"Opened the app {app[0]}"
    exe = shutil.which(t)
    if exe:
        subprocess.Popen([exe], creationflags=CREATE_NO_WINDOW)
        return f"Started {exe}"
    names = difflib.get_close_matches(low, [n.lower() for n, _ in start_apps()], n=5,
                                      cutoff=0.4)
    return (f"Couldn't find an app, file or site called '{target}'."
            + (f" Similar apps: {', '.join(names)}." if names else ""))


# ---------- system ----------
class _Power(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


class _Mem(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def powershell(command: str, timeout: int = 60) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          command], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL)
    text = (out.stdout + ("\n" + out.stderr if out.stderr.strip() else "")).strip()
    return text or f"(no output, exit code {out.returncode})"


def system_info() -> str:
    lines = [time.strftime("Time: %A %d %B %Y, %I:%M %p")]
    p = _Power()
    if kernel32.GetSystemPowerStatus(ctypes.byref(p)) and p.BatteryFlag != -128:
        plug = "plugged in" if p.ACLineStatus == 1 else "on battery"
        lines.append(f"Battery: {p.BatteryLifePercent}% ({plug})")
    m = _Mem(dwLength=ctypes.sizeof(_Mem))
    kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    lines.append(f"Memory: {m.dwMemoryLoad}% used of {m.ullTotalPhys / 2**30:.1f} GB")
    try:
        cpu = powershell("(Get-CimInstance Win32_Processor | Measure-Object "
                         "-Property LoadPercentage -Average).Average", 20)
        lines.append(f"CPU: {cpu}% busy")
    except subprocess.TimeoutExpired:
        pass
    disk = shutil.disk_usage(Path.home().anchor)
    lines.append(f"Disk {Path.home().anchor}: {disk.free / 2**30:.0f} GB free of "
                 f"{disk.total / 2**30:.0f} GB")
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    up = kernel32.GetTickCount64() // 1000
    lines.append(f"Up for {up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lines.append(f"Local IP: {s.getsockname()[0]} (online)")
        s.close()
    except OSError:
        lines.append("Network: offline")
    return "\n".join(lines)


def processes() -> str:
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                         text=True, errors="replace", creationflags=CREATE_NO_WINDOW)
    mem: dict[str, int] = {}
    for line in out.stdout.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) >= 5:
            kb = int(re.sub(r"\D", "", cols[4]) or 0)
            mem[cols[0]] = mem.get(cols[0], 0) + kb
    top = sorted(mem.items(), key=lambda kv: -kv[1])[:25]
    return "\n".join(f"{n}: {kb / 1024:.0f} MB" for n, kb in top)


# ---------- files / web ----------
def find_files(query: str, folder: str = "", limit: int = 30) -> str:
    root = Path(os.path.expanduser(folder)) if folder else Path.home()
    pattern = query if any(c in query for c in "*?") else f"*{query}*"
    rx = re.compile(re.escape(pattern).replace(r"\*", ".*").replace(r"\?", "."), re.I)
    hits, deadline = [], time.time() + 8
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames + dirnames:
            if rx.fullmatch(name):
                hits.append(os.path.join(dirpath, name))
                if len(hits) >= limit:
                    return "\n".join(hits)
        if time.time() > deadline:
            hits.append("(search stopped after 8 s)")
            break
    return "\n".join(hits) or f"No files matching '{query}' under {root}"


def list_folder(path: str) -> str:
    if path.lower() in FOLDERS:
        p = Path.home() / FOLDERS[path.lower()]
    else:
        p = Path(os.path.expanduser(path))
    items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    return "\n".join(f"{'[dir] ' if x.is_dir() else ''}{x.name}" for x in items[:200])


def web_page(url: str) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 claude-pet"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(2_000_000).decode(resp.headers.get_content_charset() or "utf-8",
                                          "replace")
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()


def read_screen_text(hwnd: int) -> str:
    """Names and values of a window's UI elements via UI Automation."""
    script = f"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                     [System.Windows.Automation.Condition]::TrueCondition)
$seen = @{{}}; $n = 0
foreach ($e in $all) {{
  if ($n -ge 1500) {{ break }}; $n++
  try {{
    $name = $e.Current.Name
    $type = $e.Current.ControlType.ProgrammaticName -replace 'ControlType\\.', ''
    $val = ''
    $vp = $null
    if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {{
      $val = $vp.Current.Value }}
    $line = (($type + ': ' + $name + $(if ($val) {{ ' = ' + $val }} else {{ '' }})) -replace '\\s+', ' ').Trim()
    if (($name -or $val) -and -not $seen.ContainsKey($line)) {{ $seen[$line] = 1; $line }}
  }} catch {{}}
}}
"""
    return powershell(script, 30)


# ---------- the dispatcher ----------
class Computer:
    """Runs tool calls with the risk check. Shared by every provider.

    confirm(summary) -> bool asks the user (blocking, any thread).
    pet(name, args) -> str handles pet-side tools (reminders, tasks).
    mode(): "trust", "ask" (the pet's default) or "off". blocked() applies in every mode.
    on_action(summary): optional, for the pet's "thinking" caption.
    """

    def __init__(self, confirm: Callable[[str], bool],
                 pet: Callable[[str, dict], str],
                 mode: Callable[[], str] = lambda: "trust",
                 on_action: Callable[[str], None] | None = None) -> None:
        self.confirm = confirm
        self.pet = pet
        self.mode = mode
        self.on_action = on_action
        self.allowed: set[str] = set()  # "Always" answers, for this pet run
        self.scale = 1  # screen pixels per pixel of the last screenshot

    def specs(self, vision: bool = True) -> list[dict]:
        if self.mode() == "off":
            return []
        if vision:
            return SPECS
        out = []
        for s in SPECS:  # models that can't see: no screenshot option
            if s["name"] == "screen":
                s = json.loads(json.dumps(s))
                s["parameters"]["properties"]["action"]["enum"] = ["read_text"]
                s["description"] = "read_text: the text and controls of a window."
            out.append(s)
        return out

    def call(self, name: str, args: dict | None) -> tuple[str, Path | None]:
        """Run one tool. Returns (text for the model, image path or None)."""
        args = dict(args or {})
        if name not in NAMES:
            return f"Unknown tool {name}.", None
        mode = self.mode()
        if mode == "off":
            return "Computer control is turned off in the pet's menu.", None
        if CANCEL.is_set():
            return "The user stopped this task. Do nothing more.", None
        summary = describe(name, args)
        point = None
        if name == "mouse" and args.get("x") is not None and args.get("y") is not None:
            point = (int(args["x"]) * self.scale, int(args["y"]) * self.scale)
        why = blocked(name, args, point)
        if why:
            if self.on_action:
                self.on_action("Refused: that would delete")
            return why, None
        key = f"{name}.{args.get('action', '')}"
        if (mode != "trust" and risk(name, args) == "ask" and key not in self.allowed):
            answer = self.confirm(summary)
            if answer == "always":
                self.allowed.add(key)
            elif not answer:
                return "The user declined this action. Don't retry it.", None
        if self.on_action:
            self.on_action(summary)
        try:
            text, image = self._run(name, args)
        except subprocess.TimeoutExpired:
            return "That took too long and was stopped.", None
        except Exception as exc:  # report, don't crash the model's turn
            return f"Error: {exc}", None
        if len(text) > OUTPUT_LIMIT:
            text = text[:OUTPUT_LIMIT] + f"\n…(cut, {len(text)} characters total)"
        return text, image

    def _run(self, name: str, a: dict) -> tuple[str, Path | None]:
        action = a.get("action", "")
        if name == "open":
            return open_target(str(a.get("target", ""))), None
        if name == "windows":
            if action == "list":
                wins = list_windows()
                return "\n".join(f"{w['title']} — {w['app']}" for w in wins) or "No windows.", None
            win = find_window(str(a.get("title", "")))
            if not win:
                return f"No open window matches '{a.get('title')}'.", None
            hwnd = win["hwnd"]
            if action == "focus":
                focus_window(hwnd)
            elif action == "close":
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            else:
                user32.ShowWindow(hwnd, {"minimize": 6, "maximize": 3, "restore": 9}[action])
            done = {"focus": "Focused", "close": "Closed", "minimize": "Minimized",
                    "maximize": "Maximized", "restore": "Restored"}[action]
            return f"{done} “{win['title']}” ({win['app']})", None
        if name == "keyboard":
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
            if pid.value == os.getpid():
                return ("The pet's own window has focus. Focus the target window "
                        "first with `windows` focus."), None
            if a.get("text"):
                type_text(str(a["text"]))
            if a.get("keys"):
                press_keys(str(a["keys"]))
            fg = find_window("")
            where = f" in “{fg['title']}”" if fg else ""
            return f"Done{where}.", None
        if name == "mouse":
            x, y = a.get("x"), a.get("y")
            if x is not None and y is not None:
                x, y = int(x) * self.scale, int(y) * self.scale
            mouse(action, x, y, int(a.get("amount") or 0))
            return "Done.", None
        if name == "screen":
            if action == "screenshot":
                path, w, h = self.screenshot()
                return (f"Screenshot of the main screen, {w}x{h} pixels (use these "
                        f"coordinates with the mouse tool): {path}"), path
            win = find_window(str(a.get("title", "")))
            if not win:
                return f"No open window matches '{a.get('title')}'.", None
            return f"Window “{win['title']}” ({win['app']}):\n" + read_screen_text(win["hwnd"]), None
        if name == "media":
            keys = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "mute": 0xAD,
                    "volume_up": 0xAF, "volume_down": 0xAE}
            if action == "set_volume":
                level = max(0, min(100, int(a.get("level") or 0)))
                seq = [0xAE] * 50 + [0xAF] * round(level / 2)  # each step is 2%
                for vk in seq:
                    _send([_key(vk, flags=1), _key(vk, flags=3)])
                return f"Volume set to about {level}%.", None
            steps = 5 if action.startswith("volume") else 1
            for _ in range(steps):
                _send([_key(keys[action], flags=1), _key(keys[action], flags=3)])
            return f"Done: {action.replace('_', ' ')}.", None
        if name == "system":
            if action == "info":
                return system_info(), None
            if action == "processes":
                return processes(), None
            if action == "kill":
                target = str(a.get("name", "")).strip()
                exe = target if target.lower().endswith(".exe") else target + ".exe"
                out = subprocess.run(["taskkill", "/im", exe, "/f"], capture_output=True,
                                     text=True, errors="replace", creationflags=CREATE_NO_WINDOW)
                return (out.stdout + out.stderr).strip(), None
            if action == "brightness":
                level = max(0, min(100, int(a.get("level") or 50)))
                return powershell("(Get-CimInstance -Namespace root/WMI -ClassName "
                                  "WmiMonitorBrightnessMethods).WmiSetBrightness(1,"
                                  f"{level}) | Out-Null; 'Brightness {level}%'", 20), None
            if action == "lock":
                user32.LockWorkStation()
                return "Locked.", None
            cmds = {"sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                    "shutdown": ["shutdown", "/s", "/t", "60"],
                    "restart": ["shutdown", "/r", "/t", "60"],
                    "cancel_shutdown": ["shutdown", "/a"]}
            subprocess.Popen(cmds[action], creationflags=CREATE_NO_WINDOW)
            return {"shutdown": "Shutting down in 60 s (say cancel to stop it).",
                    "restart": "Restarting in 60 s (say cancel to stop it).",
                    "sleep": "Going to sleep.", "cancel_shutdown": "Cancelled."}[action], None
        if name == "clipboard":
            if action == "set":
                subprocess.run(["clip"], input=str(a.get("text", "")).encode("utf-16-le"),
                               creationflags=CREATE_NO_WINDOW)
                return "Copied.", None
            return powershell("Get-Clipboard -Raw", 15), None
        if name == "files":
            path = os.path.expanduser(str(a.get("path", "")))
            if action == "find":
                return find_files(str(a.get("query", "")), path), None
            if action == "list":
                return list_folder(path or "home"), None
            if action == "read":
                return Path(path).read_text(encoding="utf-8", errors="replace"), None
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(str(a.get("content", "")), encoding="utf-8")
            return f"Wrote {path}", None
        if name == "run_command":
            return powershell(str(a.get("command", ""))), None
        if name == "web_page":
            return web_page(str(a.get("url", ""))), None
        if name == "reminder":
            return self.pet(name, a), None
        if name == "wait":
            seconds = max(0.0, min(15.0, float(a.get("seconds") or 1)))
            if CANCEL.wait(seconds):
                return "Stopped by the user.", None
            return f"Waited {seconds:g} s.", None
        return f"Unknown tool {name}.", None

    def screenshot(self) -> tuple[Path, int, int]:
        """Main screen, shrunk by a whole factor to about SHOT_MAX_W wide."""
        from snip import KEEP_SHOTS, grab, png_bytes  # local: needs no Tk
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        k = max(1, -(-w // SHOT_MAX_W))
        raw = grab(0, 0, w, h)
        if k > 1:  # keep every k-th pixel of every k-th row
            sw, sh = w // k, h // k
            rows = (raw[r * k * w * 4:(r * k * w + sw * k) * 4] for r in range(sh))
            out = bytearray(sw * sh * 4)
            for i, row in enumerate(rows):
                line = bytearray(sw * 4)
                for c in range(4):
                    line[c::4] = row[c::4 * k]
                out[i * sw * 4:(i + 1) * sw * 4] = line
            raw, w, h = bytes(out), sw, sh
        self.scale = k
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOTS_DIR / time.strftime("pc-%Y%m%d-%H%M%S.png")
        path.write_bytes(png_bytes(raw, w, h))
        for old in sorted(SHOTS_DIR.glob("pc-*.png"))[:-KEEP_SHOTS]:
            try:
                old.unlink()  # the pet's own temp screenshots, not user files
            except OSError:
                pass
        return path, w, h


# ---------- bridge: lets pet_mcp.py (run by Claude Code) reach the pet ----------
def serve(computer: Computer) -> tuple[int, str]:
    """Answer tool calls on 127.0.0.1. Returns (port, token).

    One JSON line in: {"token", "name", "args"}; one JSON line out:
    {"text", "image"} with the image as base64 PNG (or null).
    """
    token = secrets.token_hex(16)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                req = json.loads(self.rfile.readline())
            except ValueError:
                return
            if not secrets.compare_digest(str(req.get("token", "")), token):
                return
            text, image = computer.call(str(req.get("name", "")), req.get("args") or {})
            data = base64.b64encode(image.read_bytes()).decode() if image else None
            self.wfile.write(json.dumps({"text": text, "image": data}).encode() + b"\n")

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], token
