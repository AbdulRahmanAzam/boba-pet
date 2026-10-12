"""Claude Pet: an always-on-top desktop pet that chats via Claude Code (Haiku).

Click the pet (or hover and pick Type / Talk) to open the ask box. Replies
pop up in a speech bubble above the pet. Drag the pet to move it.
Right-click for the menu. Each question runs `claude -p --model haiku
--effort <level>` and resumes the same session, so the pet remembers the
conversation until you pick "New chat".

Voice (offline):
  - Mic button / Ctrl+M in the box / Ctrl+Alt+V anywhere: start or stop the
    mic. Words appear in the box live (Whisper); press Enter to send.
  - Menu "Read replies aloud": speak every reply with Windows TTS. Questions
    that used the mic are always answered aloud.
  - Say "Hey Bob" (or "Hey Babe" / "Hey Boba"): the box opens and listens;
    pause when done and the question is sent (Vosk, offline).
Run with the pet's own .venv (faster-whisper, sounddevice, vosk) for voice input.

Claude Code: the pet watches your sessions' transcripts. It looks thoughtful
while a session works, celebrates when it finishes, sweats when tests fail,
and waves with a "!" when a session waits for permission, until you switch
to that window. Menu "Usage meter" shows each AI's limits and today's tokens.
Ctrl+Alt+Space anywhere shows or hides the ask box; hold it for a second
to open the box and start listening.

The pet has no API key of its own: on first start it asks you to link an
AI (menu "AI provider"): Claude Code, Codex (ChatGPT plan), Antigravity
CLI (Google account), or a Groq / Gemini API key saved in the pet's .env file.
Every request then runs on that account's plan or credits.
"""

import ctypes
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from tkinter import filedialog, simpledialog
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

# Small footprint: one math thread per library (each BLAS/OpenMP thread
# reserves its own buffers) and no progress-bar threads from model downloads.
# Must run before numpy or any model library is imported.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")

from ui_fx import (Fader, enable_dpi_awareness, hwnd_of, lerp_color, pick_font,
                   render_markdown, round_corners, set_border, setup_markdown)

DPI_SCALE = enable_dpi_awareness()  # must run before any Tk window exists
_fast_timer = False


def fast_timer(on: bool) -> None:
    """1 ms timer resolution, only while animating, so Tk's after() can hit
    ~60 fps. At rest Windows' normal 15.6 ms tick saves power and CPU."""
    global _fast_timer
    if on == _fast_timer:
        return
    _fast_timer = on
    try:
        winmm = ctypes.windll.winmm
        (winmm.timeBeginPeriod if on else winmm.timeEndPeriod)(1)
    except (AttributeError, OSError):
        pass


def release_memory() -> None:
    """Hand unused RAM back to Windows: collect garbage, then trim the
    working set (pages come back on their own if needed)."""
    import gc

    gc.collect()
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessWorkingSetSize.argtypes = (wintypes.HANDLE, ctypes.c_size_t,
                                                      ctypes.c_size_t)
        kernel32.SetProcessWorkingSetSize(kernel32.GetCurrentProcess(),
                                          ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value)
    except (AttributeError, OSError):
        pass

from sprites import CHARACTERS, DEFAULT_CHARACTER, SIZE, SpriteCache, compose  # noqa: E402

from tasks import (DEFAULT_DISTRACTIONS, FocusGuard, TaskStore,  # noqa: E402
                   display_name, minimize)
import hackathons  # noqa: E402
from focus import BREAK_SECONDS, FOCUS_SECONDS, EdgeGlow, FocusRing  # noqa: E402
from snip import SnipOverlay, save_shot  # noqa: E402
import projects  # noqa: E402
import account  # noqa: E402
import providers  # noqa: E402
import cc_watch  # noqa: E402
import computer  # noqa: E402
import usage  # noqa: E402
import chatlog  # noqa: E402
import news  # noqa: E402
import hackmode  # noqa: E402
import pet_paths  # noqa: E402
import water  # noqa: E402
import keys  # noqa: E402
import updater  # noqa: E402

try:  # drag & drop of files / text onto the pet
    from tkinterdnd2 import DND_FILES, DND_TEXT, TkinterDnD
except ImportError:
    TkinterDnD = None

try:
    from listener import LiveListener, preload as preload_whisper
except ImportError:  # faster-whisper / sounddevice missing: typing still works
    LiveListener = None
    preload_whisper = None

try:
    import wake
except ImportError:  # numpy missing: no wake word
    wake = None

MODEL = "haiku"  # default chat model, and the one for cheap background calls
CLAUDE_MODELS = ("haiku", "sonnet", "opus")  # Claude Code aliases: newest of each
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "low"

# Token diet for the pet only (your normal Claude Code setup is untouched).
# Measured on a "hi": ~29.2k input tokens with everything, ~12.8k with this.
# - Plugins off: drops the design plugin's MCP servers (Slack, Figma, Notion,
#   Linear, Atlassian, Intercom), its skills, and per-prompt hook text.
#   claude.ai connectors (Claude Docs, Gmail) stay.
# - Only the tools a desktop helper needs; skill listing off.
# - Short replies (output tokens cost the most), compact long chats early,
#   and start a fresh chat after a long idle so history stops piling up.
PET_SETTINGS = json.dumps({"enabledPlugins": {
    "design@synced": False,
    "cowork-plugin-management@synced": False,
    "caveman@caveman": False,
    "savvy-flow@azam-local": False,
}})
PET_TOOLS = "PowerShell,Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,ToolSearch"
PET_PROMPT = ("You are a desktop pet assistant. Reply short, in plain text, "
              "with minimal markdown. Use tools only when needed.")

# Two-tier routing: tool definitions (~12k tokens) load only when needed.
# 1. "lite" call: no tools, no MCP, tiny system prompt (~0.7k tokens).
#    If it needs tools it answers only ESCALATE.
# 2. Then the same session continues in "tools" mode with a one-line nudge,
#    so the question is never sent twice.
# Obvious tool requests (KEYWORDS, or a "/t " prefix) skip straight to tools.
ESCALATE = "NEED_TOOLS"
LITE_PROMPT = (
    "You are a desktop pet assistant. Today is {today}. You have no tools "
    "right now. If a correct answer needs live or recent info, the web, files "
    "or apps on this PC, running commands, email, or Claude Docs, reply "
    f"exactly {ESCALATE} and nothing else. Otherwise reply short, in plain "
    "text, minimal markdown.")
ESCALATE_NUDGE = "Go ahead, use your tools."
# A chat started in lite mode keeps answering NEED_TOOLS in tools mode unless
# the new message says the tools are on now.
TOOLS_ON_PREFIX = "(Your tools are enabled now.) "
# Computer mode: an old chat may hold "I can't, PowerShell is disabled"; only
# a note in the message itself (not the system prompt) overrides that.
PC_ON_PREFIX = ("(Your PC tools mcp__pc__* work now; PowerShell is not needed. "
                "Use them.) ")
FORCE_PREFIX = "/t "
# Only unambiguous signals: a wrong hit costs ~12k tokens, a miss only ~0.7k.
KEYWORDS = re.compile(
    r"\b(e-?mails?|gmail|inbox|claude docs|look\s*up|google it|"
    r"search (the )?(web|internet|online)|"
    r"my (files?|folders?|downloads|desktop|docs?|documents|pc|computer))\b"
    r"|https?://|www\.|\b[a-z]:\\", re.I)
# Requests to operate the PC skip the lite route (menu "Computer control").
COMPUTER_WORDS = re.compile(
    r"\b(open|launch|close|click|press|type|write .{1,40} (in|to|on)|send|whatsapp|"
    r"chrome|edge|browser|spotify|youtube|volume|brightness|screenshot|screen|"
    r"windows?|minimi[sz]e|maximi[sz]e|tab|app|chat (of|with)|message|battery|"
    r"memory|ram|cpu|disk|storage|wi-?fi|internet|processes|clipboard|remind|"
    r"files?|folders?|downloads?|lock|sleep|shut ?down|restart|mute|play|pause)\b", re.I)
# Computer mode: the pet's guarded tools replace PowerShell and Write, so
# nothing can be deleted or overwritten through Claude Code's own tools.
PC_TOOLS = "Read,Edit,Glob,Grep,WebSearch,WebFetch,ToolSearch"
COMPUTER_MODES = (("trust", "Do everything (never delete)"),
                  ("ask", "Ask before risky actions"),
                  ("off", "Off"))
AUTOCOMPACT_TOKENS = "100k"  # CLI minimum
IDLE_RESET_SECONDS = 30 * 60
APP_DIR = Path(__file__).resolve().parent
SPEAK_SCRIPT = APP_DIR / "speak.ps1"
STATE_FILE = Path.home() / ".claude-pet.json"
WORK_DIR = Path.home()

# ---------- look ----------
TRANSPARENT = "#010203"
ACCENT = "#B9A3E3"  # light purple, same as Shellby the snail
ACCENT_DARK = "#4A3F66"  # text selection
ACCENT_HOVER = "#CDBDF0"
RED = "#E5484D"
RED_SOFT = "#FF9A9A"
MUTED = "#8A8780"
PANEL_BG = "#1F1E1D"
PANEL_FG = "#ECEAE4"
INPUT_BG = "#2B2A28"
HOVER_BG = "#34322F"
BORDER = "#3D3935"
SHADOW = "#141316"
RIM = "#E4DAFB"  # thin light outline around the pet, for dark wallpapers

# ---------- pet geometry & motion ----------
# Pet size (menu "Size"): scales the sprite pixel size. Read before any
# window exists, because every layout size below derives from ZOOM.
PET_SIZES = {"Small": 2 / 3, "Normal": 1.0, "Large": 4 / 3, "Extra large": 5 / 3,
             "Huge": 2.0}
try:
    PET_SIZE = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("size", "Normal")
except (OSError, ValueError, AttributeError):
    PET_SIZE = "Normal"
if PET_SIZE not in PET_SIZES:
    PET_SIZE = "Normal"
ZOOM = max(2, round(3 * DPI_SCALE * PET_SIZES[PET_SIZE]))  # sprite pixel size on screen
SPRITE_PX = SIZE * ZOOM
SWAY_ROOM = 3 * ZOOM  # side room for drag sway / shake
HOP_ROOM = 5 * ZOOM  # headroom for hops
SHADOW_ROOM = 2 * ZOOM
PET_W = SPRITE_PX + 2 * SWAY_ROOM
PET_H = HOP_ROOM + SPRITE_PX + SHADOW_ROOM
TICK_MS = 16  # ~60 fps, only while something moves
IDLE_TICK_MS = 100  # awake but calm
IDLE_SLEEP_MS = 200  # asleep (also how fast hotkeys are picked up)
MEMORY_POLL_MS = 60_000  # check for models to unload / RAM to give back
SPRITE_FPS = 16.7  # pose timing base (sprites were authored at this rate)
SLEEP_AFTER_SECONDS = 3 * 60
# idle strolls along the taskbar
WALK_EVERY = (30, 45)  # seconds between strolls
WALK_SPEED = 60 * DPI_SCALE  # screen px per second
WALK_STEP_HZ = 3.4  # little bounces per second while walking
WALK_GRAVITY = 2600 * DPI_SCALE  # px/s^2 when hopping down to the taskbar
WALK_USER_IDLE_MAX = 60  # only stroll while you're using the PC

BAR_CHARS = 34
BAR_MAX_LINES = 6  # ask box grows downward up to this, then scrolls
BUBBLE_CHARS = 44
BUBBLE_MIN_CHARS = 16
BUBBLE_MAX_LINES = 14
REMIND_CHOICES = (15, 30, 45, 60, 0)  # minutes; 0 = off
DEFAULT_REMIND_MINUTES = 45
FOCUS_CHOICES = (15, 25, 30, 45, 60, 90)  # menu presets; any length works
FOCUS_MIN_MINUTES, FOCUS_MAX_MINUTES = 1, 240
TASK_POLL_MS = 5000
TASKS_WIDTH = 340
# Claude Code alerts: pet_hook.py (a Stop/Notification hook) appends events
# here; the pet watches the file.
CC_EVENTS_FILE = Path.home() / ".claude-pet-events.jsonl"
CC_POLL_MS = 700
CC_MIN_TURN_SECONDS = 20  # quick replies you watched happen don't alert
WATCH_POLL_SECONDS = 1.5  # transcript watcher
WAIT_MAX_SECONDS = 15 * 60  # stop waving about a permission prompt after this
WAVE_HOP_SECONDS = 2.2
USAGE_POLL_SECONDS = 60
USAGE_COLORS = ((60, ("#6EE7B7", "#ECFDF5", "#24493E")),  # (below %, colors)
                (80, ("#FDE68A", "#FFFBEB", "#4A4122")),
                (101, ("#FCA5A5", "#FEF2F2", "#4E2626")))
WAKE_SILENCE_MS = 8000  # after "Hey Bob": give up if nothing is said
# Scoped calls (dropped files, screenshots, briefing): only the tools named,
# a tiny system prompt and no MCP, so they cost far less than tools mode.
SCOPED_PROMPT = ("You are a desktop pet assistant. Today is {today}. Reply short, "
                 "in plain text, with minimal markdown.")
SHOTS_DIR = pet_paths.SHOTS_DIR
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
DROP_TEXT_LIMIT = 20_000  # characters of dropped text sent to Claude
DROP_ACTIONS = {
    "Explain": "Explain this clearly and briefly.",
    "Summarize": "Summarize this in a few short bullet points.",
    "Fix": ("Find mistakes or bugs and show the corrected version. "
            "Do not edit any files."),
}
SHOT_ACTIONS = {
    "Explain": "What is in this screenshot? Explain it briefly.",
    "Extract text": "Extract all text in this image exactly as written.",
}
# Morning briefing: first activity of the day, after BRIEF_HOUR.
BRIEF_HOUR = 5
BRIEF_POLL_MS = 60_000
BRIEF_IDLE_MAX = 120  # seconds since last keyboard/mouse input = "you're here"
# After the morning briefing, news updates every NEWS_GAP_S, NEWS_PER_DAY in all.
NEWS_PER_DAY = 3
NEWS_GAP_S = 4 * 3600
NEWS_UPDATE_COUNT = 3  # items in a later update (the morning one has 5)
STREAM_FRAME_S = 0.045  # redraw streamed text at most ~22 times a second


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input anywhere."""
    info = _LastInput(ctypes.sizeof(_LastInput), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def cc_find_window(pids: list[int]) -> int:
    """Visible top-level window of the nearest ancestor that owns one.

    `pids` is the hook's parent chain (claude, shell, ..., terminal app), so
    the first owner found is the terminal or editor the session runs in.
    """
    user32 = ctypes.windll.user32
    owners: dict[int, int] = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _lparam: int) -> bool:
        if (user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4)
                and user32.GetWindowTextLengthW(hwnd)):  # unowned, has a title
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            owners.setdefault(pid.value, hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return next((owners[p] for p in pids if p in owners), 0)


def tool_caption(name: str, args: dict) -> str:
    """Short, friendly caption for a tool Claude is running."""
    def short(text: str, n: int = 28) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= n else text[:n - 1] + "…"

    def file_name(path: str) -> str:
        return short(str(path).replace("\\", "/").rsplit("/", 1)[-1], 24)

    low = name.lower()
    if name == "WebSearch":
        q = args.get("query")
        return f"Searching “{short(q)}”" if q else "Searching the web"
    if name == "WebFetch":
        url = str(args.get("url", ""))
        host = re.sub(r"^https?://(www\.)?", "", url).split("/", 1)[0]
        return f"Reading {short(host)}" if host else "Reading a page"
    if name == "Read":
        f = args.get("file_path")
        return f"Reading {file_name(f)}" if f else "Reading a file"
    if name in ("Edit", "Write"):
        f = args.get("file_path")
        verb = "Editing" if name == "Edit" else "Writing"
        return f"{verb} {file_name(f)}" if f else f"{verb} a file"
    if name in ("Glob", "Grep"):
        pat = args.get("pattern")
        return f"Looking for {short(pat, 22)}" if pat else "Looking through files"
    if name in ("PowerShell", "Bash"):
        return "Running a command"
    if name == "ToolSearch":
        return "Picking the right tool"
    if "gmail" in low:
        return "Checking Gmail"
    if "docs" in low:
        return "Opening Claude Docs"
    return f"Using {short(name.split('__')[-1], 20)}"


GAZE_HOLD_SECONDS = 4  # pointer still this long: eyes drift back to center
GAZE_DEADZONE = 7  # sprite pixels around the eyes where it looks straight on
CARET_GAP = 6  # hint text starts after the blinking caret, never on top of it
CARET = dict(insertbackground="#ECEAE4", insertwidth=2,
             insertontime=530, insertofftime=530)  # standard Windows blink
TASKS_MAX_LIST_PX = 300
HACKS_WIDTH = 380
HACKS_MAX_LIST_PX = 420
HACK_BAR_SEGMENTS = 48
# Same colors as the hack-clock strip above the Claude Code prompt.
HACK_FROM, HACK_TO, HACK_GLOW = "#8B5CF6", "#F0ABFC", "#FAF5FF"
HACK_RED_FROM, HACK_RED_TO, HACK_RED_GLOW = "#F87171", "#DC2626", "#FFF1F2"
HACK_DONE_FROM, HACK_DONE_TO, HACK_DONE_GLOW = "#10B981", "#6EE7B7", "#ECFDF5"
HACK_TRACK = "#3B2A5C"
HACK_GREEN = "#6EE7B7"
CREATE_NO_WINDOW = 0x08000000
POWERSHELL = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]

# Global hotkeys (Win32 RegisterHotKey)
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
VK_SPACE, VK_V = 0x20, 0x56
WM_HOTKEY = 0x0312
HOTKEY_TOGGLE, HOTKEY_TALK = 1, 2
VK_S = 0x53
HOTKEY_SHOT = 3
VK_L = 0x4C
HOTKEY_CONT = 4
# Continuous voice (Ctrl+Alt+L): the mic stays open, each spoken command is
# sent after a short pause, and speaking while the pet works stops it.
CONT_SEND_MS = 900  # quiet time after a phrase before it is sent
STOP_WORDS = re.compile(
    r"^\W*(stop|stop it|wait|cancel|pause|hold on|enough|no|leave it|ruko|ruk jao|"
    r"bas|bas karo|nahi|band karo)\W*$", re.I)
STOP_IN_PARTIAL = re.compile(r"\b(stop|ruko|cancel)\b", re.I)
# Whisper "hears" these in silence or noise; never treat them as commands.
NOISE_PHRASES = {"", "you", "thank you", "thanks", "bye", "so", "uh", "um", "hmm",
                 "thanks for watching", "thank you for watching", "subscribe",
                 "please subscribe", "okay", "ok", "oh", "ah", "yeah"}
HOLD_TO_TALK = 1.0  # seconds Ctrl+Alt+Space is held before listening starts


def find_claude() -> str:
    """Locate the Claude Code executable."""
    found = shutil.which("claude")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "claude.exe")


def claude_usage(result: dict, context: int = 0) -> dict:
    """Tokens of one `claude -p` call from its result message.

    "in" counts everything sent (fresh, cache writes and cache reads), "out"
    what the model wrote, "context" the size of the last model call.
    """
    u = result.get("usage") or {}
    sent = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            + u.get("cache_read_input_tokens", 0))
    window = max((m.get("contextWindow") or 0
                  for m in (result.get("modelUsage") or {}).values()), default=0)
    return {"in": sent, "out": u.get("output_tokens", 0),
            "context": context or sent + u.get("output_tokens", 0), "window": window}


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def speech_text(markdown: str) -> str:
    """Strip markdown so text-to-speech does not read symbols aloud."""
    text = re.sub(r"```.*?```", " (code omitted) ", markdown, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^[#>\-*+\s]+", "", text, flags=re.M)
    text = re.sub(r"[*_~|]", "", text)
    return text[:6000]


def work_area() -> tuple[int, int, int, int]:
    """Screen area not covered by the taskbar: (left, top, right, bottom)."""
    r = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


def feet_of(character: str) -> tuple[int, int, int]:
    """(lowest row, left col, right col) of a character's idle pose."""
    grid = compose(character)
    rows = [y for y, row in enumerate(grid) if any(row)]
    cols = [x for row in grid for x, c in enumerate(row) if c]
    return (rows[-1] if rows else SIZE - 1,
            min(cols) if cols else 0, max(cols) if cols else SIZE - 1)


class ClaudePet:
    def __init__(self) -> None:
        self.state = load_state()
        self.session_id: str | None = self.state.get("session_id")
        self.chat = chatlog.ChatLog()  # messages, tokens and summary of this chat
        self.hack_data = hackmode.load()  # per-hackathon workspaces, active one
        self.hack_view: str | None = None  # hackathon whose workspace is open
        self.hack_coach_last = 0.0
        self.hack_coach_dirs: list[str] = []  # read by the watcher thread
        self.hack_milestone_check = 0.0
        self.hack_linking = False  # project picker open in the workspace
        self.compacting = False
        self.pending_prompt = ""  # the request running now, kept on success
        self.effort: str = self.state.get("effort", DEFAULT_EFFORT)
        self.claude_model: str = self.state.get("claude_model", MODEL)
        if self.claude_model not in CLAUDE_MODELS:
            self.claude_model = MODEL
        self.claude = find_claude()
        self.provider: str = self.state.get("provider", "claude")
        if self.provider == "gemini-cli":  # Gemini CLI was replaced by Antigravity
            self.provider = "antigravity"
        if self.provider not in providers.PROVIDERS:
            self.provider = "claude"
        # Claude Code link: None until checked, then account.status() output
        self.account: dict | None = None
        self.link_polling = False
        self.events: queue.Queue[Callable[[], None]] = queue.Queue()
        self.busy = False
        self.busy_since = 0.0
        self.busy_label = "Thinking"
        self.unread = False
        # streaming reply: the text so far, what is on screen, redraw timing
        self.thinking_prompt = ""
        self.stream_view = False
        self.stream_pending: str | None = None
        self.stream_shown = ""
        self.stream_drawn_at = 0.0
        self.stream_width = 0
        self.listening = False
        self.listener: "LiveListener | None" = None
        self.listen_gen = 0
        self.voice_base = ""
        self.voice_used = False
        self.finishing = False
        self.send_pending = False
        self.speak_proc: subprocess.Popen | None = None
        self.last_reply = ""
        # animation state (time based, so smooth at any refresh rate)
        self.t0 = time.perf_counter()
        self.blink_until = 0.0
        self.next_blink = self.t0 + random.uniform(2, 5)
        self.hat_tip_until = 0.0
        # eyes follow the mouse: last pointer spot, when it last moved,
        # and a short debounce so the pupils don't flicker on boundaries
        self.pointer = (0, 0)
        self.pointer_moved = 0.0
        self.gaze_now = "open"
        self.gaze_next = ("open", 0.0)
        self.happy_until = 0.0
        self.hop: tuple[float, float, float] | None = None  # start, dur, height
        self.shake: tuple[float, float] | None = None  # start, dur
        self.sway = 0.0
        self.sway_target = 0.0
        self.last_drawn: tuple = ()
        self.last_active = time.time()
        self.drag_start: tuple[int, int] | None = None
        self.drag_last: tuple[int, float] = (0, 0.0)
        self.moved = False
        self.reveal_total = 0
        self.reveal_shown = 0
        self.reveal_start = 0.0
        self.reveal_dur = 0.0
        self.feet_cache: dict[str, tuple[int, int, int]] = {}
        self.tasks = TaskStore(self.state, lambda: save_state(self.state))
        self.hack_bars: list[tuple[tk.Canvas, list[int], list[str], str]] = []
        self.hack_clocks: list[tuple[tk.Label, dict]] = []
        self.hack_mtime = 0.0
        self.hack_removing: str | None = None
        self.hack_adding = False
        self.hack_paste_url: str | None = None
        self.hack_paste_focus = ""  # which competition, for a page that lists several
        self.guard = FocusGuard(self.state.get("distractions", DEFAULT_DISTRACTIONS))
        self.last_reminder = time.time()
        self.reminder_hide_job: str | None = None
        # focus timer: {"mode": "focus"|"break", "task_id", "text", "start", "end"}
        self.focus: dict | None = self.state.get("focus")
        # computer control: the AI operates the PC through computer.py, never deleting
        self.computer_mode: str = self.state.get("computer_mode", "ask")
        self.computer = computer.Computer(
            confirm=self.confirm_action, pet=self.pet_tool,
            mode=lambda: self.computer_mode,
            on_action=lambda summary: self.post(lambda: self.computer_step(summary)))
        self.talk_back = False  # this request answers out loud (voice / continuous)
        self.spoke_plan = False  # ...and already said what it is about to do
        self.echo_until = 0.0  # mic ignores the pet's own voice until then
        self.bridge = computer.serve(self.computer)
        self.claude_proc: subprocess.Popen | None = None  # the running request
        self.work_gen = 0  # one number per request, to spot interrupted ones
        self.stopped_gen = -1
        # continuous voice
        self.cont: "LiveListener | None" = None
        self.cont_gen = 0
        self.cont_final = ""  # everything heard this session
        self.cont_used = 0  # how much of cont_final was already handled
        self.cont_job: str | None = None
        self.cont_interrupted = False

        self.root = tk.Tk()
        self.root.title("Claude Pet")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.configure(bg=TRANSPARENT)
        self.fx = Fader(self.root)
        family = pick_font("Segoe UI Variable Text", "Segoe UI")
        self.font = (family, 10)
        self.font_small = (family, 8)
        self.font_bold = (family, 10, "bold")

        self.effort_var = tk.StringVar(value=self.effort)
        self.speak_var = tk.BooleanVar(value=self.state.get("speak", False))
        self.tools_var = tk.BooleanVar(value=self.state.get("always_tools", False))
        self.guard_var = tk.BooleanVar(value=self.state.get("focus_guard", True))
        self.water_var = tk.BooleanVar(value=self.state.get("water", True))
        self.water_next = time.time() + water.INTERVAL_SECONDS
        self.keys_var = tk.BooleanVar(value=self.state.get("key_counter", True))
        self.key_counter = keys.Counter()
        self.keys_saved = time.time()
        if self.keys_var.get():
            self.key_counter.start()
        self.app_version = updater.installed_version()
        self.update_checking = False
        self.root.after(4000, self.announce_update)
        self.root.after(updater.FIRST_CHECK_DELAY_MS, self.update_poll)
        self.rim_var = tk.BooleanVar(value=self.state.get("rim", True))
        self.cc_var = tk.BooleanVar(value=self.state.get("cc_alerts", True))
        self.brief_var = tk.BooleanVar(value=self.state.get("briefing", True))
        self.walk_var = tk.BooleanVar(value=self.state.get("walk", True))
        # gravity on: the pet drops to the taskbar after you let go of it;
        # off: it stays wherever you put it (and strolls at that height)
        self.gravity_var = tk.BooleanVar(value=self.state.get("gravity", True))
        self.size_var = tk.StringVar(value=PET_SIZE)
        self.watch_var = tk.BooleanVar(value=self.state.get("cc_watch", True))
        self.usage_var = tk.BooleanVar(value=self.state.get("usage_ring", True))
        self.wake_var = tk.BooleanVar(value=self.state.get("wake_word", True))
        self.editor_var = tk.StringVar(value=self.state.get("editor", "insiders"))
        self.project_list: list[dict] | None = None
        self.project_lock = threading.Lock()
        self.searcher = projects.Searcher()
        # current stroll: {"x", "y", "floor", "vy", "legs", "dir", "t", "pause_until"}
        self.walk: dict | None = None
        self.next_walk = time.time() + random.uniform(*WALK_EVERY)
        self.attachment: dict | None = None
        self.drop_hover = False
        self.snipping = False
        self.briefing = False
        self.brief_session: str | None = None
        self.strict_var = tk.BooleanVar(value=self.state.get("strict_focus", True))
        self.provider_var = tk.StringVar(value=self.provider)
        self.groq_model_var = tk.StringVar(
            value=self.state.get("groq_model", providers.GROQ_MODELS[0]))
        self.remind_var = tk.IntVar(value=self.state.get("remind_minutes",
                                                         DEFAULT_REMIND_MINUTES))
        self.focus_min_var = tk.IntVar(
            value=self.clamp_focus(self.state.get("focus_minutes",
                                                  FOCUS_SECONDS // 60)))
        character = self.state.get("character", DEFAULT_CHARACTER)
        self.character_var = tk.StringVar(
            value=character if character in CHARACTERS else DEFAULT_CHARACTER)
        self.computer_var = tk.StringVar(value=self.computer_mode)

        left, top, right, bottom = work_area()
        x = self.state.get("x", right - PET_W - 40)
        y = self.state.get("y", bottom - PET_H - 60)
        x = min(max(x, left), right - PET_W)
        y = min(max(y, top), bottom - PET_H)
        self.root.geometry(f"{PET_W}x{PET_H}+{x}+{y}")

        self.canvas = tk.Canvas(self.root, width=PET_W, height=PET_H,
                                bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack()
        self.sprites = SpriteCache(
            TRANSPARENT, scale=ZOOM, rim_px=max(1, round(DPI_SCALE)),
            rim=RIM if self.state.get("rim", True) else None)
        self.shadow_item = self.canvas.create_oval(0, 0, 0, 0, fill=SHADOW,
                                                   outline="")
        self.sprite_item = self.canvas.create_image(0, 0, anchor="nw")
        self.badge_item = self.canvas.create_oval(0, 0, 0, 0, fill=RED,
                                                  outline="", state="hidden")
        self.badge_text = self.canvas.create_text(
            0, 0, text="!", fill="white", font=(family, 9, "bold"), state="hidden")
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.show_menu)
        self.canvas.bind("<Enter>", lambda _e: self.hover_enter())
        self.canvas.bind("<Leave>", lambda _e: self.hover_leave())

        self.build_menu()
        self.build_bar()
        self.build_drop()
        self.build_hover()
        self.build_bubble()
        self.build_history()
        self.build_tasks()
        self.build_hacks()
        self.build_hack_badge()
        self.build_focus()
        self.root.after(TASK_POLL_MS, self.task_poll)
        self.root.after(1000, self.hack_poll)
        self.cc_offset = self.cc_start_offset()
        self.cc_queue: list[dict] = []
        self.cc_last_stop: dict[str, float] = {}
        self.root.after(CC_POLL_MS, self.cc_poll)
        # Claude Code session watcher (transcripts) and permission ping
        self.watching = self.watch_var.get()  # plain copy for the worker thread
        self.cc_running: list[dict] = []  # sessions with a turn in progress
        self.cc_writes: dict[str, float] = {}  # session: last transcript write
        self.cc_waiting: dict[str, dict] = {}  # session: permission prompt
        self.cc_pids: dict[str, list] = {}  # session: hook's process chain
        self.celebrated: dict[str, float] = {}
        self.worried_until = 0.0
        self.sparkle_until = 0.0
        self.next_wave_hop = 0.0
        threading.Thread(target=self.watch_loop, daemon=True).start()
        # usage meter: ring on hover, panel, warnings near a limit
        self.usage: dict = {}
        self.usage_warned: list[str] = list(self.state.get("usage_warned", []))
        self.usage_now = threading.Event()  # wakes the usage thread early
        self.usage_ring = FocusRing(self.root, TRANSPARENT, round(SPRITE_PX * 0.64),
                                    max(2, round(2 * DPI_SCALE)),
                                    (self.font[0], 8, "bold"), PANEL_BG, PANEL_FG)
        self.usage_ring.bind_click(self.show_usage)
        self.place_ring()
        threading.Thread(target=self.usage_loop, daemon=True).start()
        # wake word ("Hey Bob")
        self.wake: "wake.WakeWord | None" = None
        self.wake_session = False  # the current mic use came from the wake word
        self.wake_job: str | None = None
        self.root.after(4000, self.start_wake)
        self.build_attach_chip()
        self.enable_drops()
        self.root.after(1500, self.check_link)
        self.root.after(15_000, self.brief_poll)
        self.root.after(20_000, lambda: threading.Thread(
            target=self.warm_projects, daemon=True).start())
        self.start_hotkeys()
        # Whisper is not preloaded: it loads when the ask box opens or the mic
        # starts, and unloads after 10 idle minutes (see free_idle_memory).
        self.memory_trimmed = self.startup_trimmed = False
        self.root.after(MEMORY_POLL_MS, self.free_idle_memory)
        self.tick()
        self.keep_on_top()
        self.start_hop(0.5, 4 * ZOOM)  # little hello on startup
        self.root.after(1200, self.drop_to_floor)

    def post(self, fn: Callable[[], None]) -> None:
        """Run fn on the Tk thread (safe to call from worker threads)."""
        self.events.put(fn)

    def popup(self) -> tuple[tk.Toplevel, tk.Frame]:
        """Borderless always-on-top window with Win11 rounded corners."""
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=PANEL_BG)
        win.withdraw()
        round_corners(win, BORDER)
        inner = tk.Frame(win, bg=PANEL_BG)
        inner.pack(fill="both", expand=True)
        return win, inner

    def icon_button(self, parent: tk.Widget, text: str,
                    action: Callable[[], None], font: tuple | None = None,
                    fg: str = MUTED, hover_fg: str = PANEL_FG,
                    **pack: object) -> tk.Label:
        """Flat label button with a hover highlight."""
        label = tk.Label(parent, text=text, bg=parent["bg"], fg=fg,
                         font=font or self.font_small, cursor="hand2",
                         padx=6, pady=2)
        label.pack(**pack)
        label.bind("<Enter>", lambda _e: label.configure(fg=hover_fg, bg=HOVER_BG))
        label.bind("<Leave>", lambda _e: label.configure(fg=fg, bg=parent["bg"]))
        label.bind("<Button-1>", lambda _e: action())
        return label

    # ---------- menu ----------
    def build_menu(self) -> None:
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Ask", accelerator="Ctrl+Alt+Space",
                              command=self.toggle_bar)
        self.menu.add_command(label="Talk", accelerator="Ctrl+Alt+V",
                              command=self.talk)
        self.menu.add_command(label="Screenshot ask", accelerator="Ctrl+Alt+S",
                              command=self.snip)
        self.cont_var = tk.BooleanVar(value=False)
        self.menu.add_checkbutton(label="Continuous voice (talk to stop)",
                                  accelerator="Ctrl+Alt+L", variable=self.cont_var,
                                  command=self.on_cont_toggle)
        self.menu.add_command(label="Tasks", command=self.toggle_tasks)
        project_menu = tk.Menu(self.menu, tearoff=0)
        project_menu.add_command(label="Rescan projects", command=self.rescan_projects)
        project_menu.add_separator()
        for label, value in (("Editor: auto (the one running)", "auto"),
                             ("Editor: VS Code", "stable"),
                             ("Editor: VS Code Insiders", "insiders")):
            project_menu.add_radiobutton(
                label=label, value=value, variable=self.editor_var,
                command=lambda: self.save_flag("editor", self.editor_var))
        self.menu.add_cascade(label="Projects", menu=project_menu)
        self.menu.add_command(label="News briefing now",
                              command=lambda: self.start_briefing())
        self.menu.add_command(label=self.focus_menu_label(),
                              command=self.focus_menu)
        self.focus_menu_index = self.menu.index("end")
        lengths = tk.Menu(self.menu, tearoff=0)
        for minutes in FOCUS_CHOICES:
            lengths.add_radiobutton(label=f"{minutes} min", value=minutes,
                                    variable=self.focus_min_var,
                                    command=lambda: self.set_focus_minutes(
                                        self.focus_min_var.get()))
        lengths.add_separator()
        lengths.add_command(label="Custom…", command=self.custom_focus_minutes)
        self.menu.add_cascade(label="Focus length", menu=lengths)
        self.menu.add_command(label="Hackathons", command=self.toggle_hacks)
        self.menu.add_command(label="Hackathon mode off",
                              command=lambda: self.set_hack_mode(None))
        self.menu.add_command(label="Chat history", command=self.toggle_history)
        self.menu.add_command(label="New chat", command=self.new_chat)
        self.menu.add_command(label="Compact chat (fewer tokens)",
                              command=self.compact_chat)
        self.menu.add_command(label="Copy last reply", command=self.copy_reply)
        self.menu.add_separator()
        self.menu.add_cascade(label="Effort", menu=self.effort_menu())
        self.model_menu = tk.Menu(self.menu, tearoff=0, postcommand=self.fill_model_menu)
        self.menu.add_cascade(label="Model", menu=self.model_menu)
        characters = tk.Menu(self.menu, tearoff=0)
        for name in CHARACTERS:
            characters.add_radiobutton(label=name, value=name,
                                       variable=self.character_var,
                                       command=self.on_character)
        self.menu.add_cascade(label="Character", menu=characters)
        sizes = tk.Menu(self.menu, tearoff=0)
        for name in PET_SIZES:
            sizes.add_radiobutton(label=name, value=name, variable=self.size_var,
                                  command=self.on_size)
        self.menu.add_cascade(label="Size", menu=sizes)
        self.menu.add_checkbutton(label="Light outline (dark wallpapers)",
                                  variable=self.rim_var, command=self.on_rim_toggle)
        self.menu.add_checkbutton(label="Walk along the taskbar when idle",
                                  variable=self.walk_var, command=self.on_walk_toggle)
        self.menu.add_checkbutton(label="Gravity (drop to the taskbar)",
                                  variable=self.gravity_var,
                                  command=self.on_gravity_toggle)
        pc_menu = tk.Menu(self.menu, tearoff=0)
        for value, label in COMPUTER_MODES:
            pc_menu.add_radiobutton(label=label, value=value, variable=self.computer_var,
                                    command=self.on_computer_mode)
        self.menu.add_cascade(label="Computer control", menu=pc_menu)
        self.menu.add_checkbutton(label="Always use tools (more tokens)",
                                  variable=self.tools_var,
                                  command=self.on_tools_toggle)
        self.menu.add_checkbutton(label="Read replies aloud",
                                  variable=self.speak_var,
                                  command=self.on_speak_toggle)
        self.menu.add_command(label="Stop speaking", command=self.stop_speaking)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="News briefings (3 a day)",
                                  variable=self.brief_var,
                                  command=lambda: self.save_flag("briefing",
                                                                 self.brief_var))
        self.menu.add_checkbutton(label="Claude Code alerts",
                                  variable=self.cc_var,
                                  command=lambda: self.save_flag("cc_alerts",
                                                                 self.cc_var))
        self.menu.add_checkbutton(label="React to Claude Code sessions",
                                  variable=self.watch_var,
                                  command=self.on_watch_toggle)
        self.menu.add_checkbutton(label='Wake word ("Hey Bob")',
                                  variable=self.wake_var,
                                  command=self.on_wake_toggle)
        usage_menu = tk.Menu(self.menu, tearoff=0)
        usage_menu.add_command(label="Show usage", command=self.show_usage)
        usage_menu.add_checkbutton(label="Usage ring when hovering the pet",
                                   variable=self.usage_var,
                                   command=lambda: self.save_flag("usage_ring",
                                                                  self.usage_var))
        usage_menu.add_command(label="Connect Claude Code limits (status line)",
                               command=self.connect_status_line)
        self.menu.add_cascade(label="Usage meter", menu=usage_menu)
        keys_menu = tk.Menu(self.menu, tearoff=0)
        keys_menu.add_command(label="Show keystroke stats", command=self.show_keys)
        keys_menu.add_checkbutton(label="Count keystrokes", variable=self.keys_var,
                                  command=self.on_keys_toggle)
        self.menu.add_cascade(label="Keystrokes", menu=keys_menu)
        self.menu.add_command(
            label=f"Check for updates (v{self.app_version})" if self.app_version
            else "Check for updates", command=lambda: self.update_check(manual=True))
        self.menu.add_checkbutton(label="Focus guard (watch for distractions)",
                                  variable=self.guard_var,
                                  command=lambda: self.save_flag("focus_guard",
                                                                 self.guard_var))
        self.menu.add_checkbutton(label="Strict mode (minimize distractions)",
                                  variable=self.strict_var,
                                  command=lambda: self.save_flag("strict_focus",
                                                                 self.strict_var))
        self.menu.add_checkbutton(label="Water reminder (every hour)",
                                  variable=self.water_var, command=self.on_water_toggle)
        remind = tk.Menu(self.menu, tearoff=0)
        for minutes in REMIND_CHOICES:
            remind.add_radiobutton(label=f"{minutes} min" if minutes else "Off",
                                   value=minutes, variable=self.remind_var,
                                   command=lambda: self.save_flag("remind_minutes",
                                                                  self.remind_var))
        self.menu.add_cascade(label="Remind me about tasks every", menu=remind)
        self.menu.add_separator()
        self.ai_menu = tk.Menu(self.menu, tearoff=0, postcommand=self.fill_ai_menu)
        self.menu.add_cascade(label="AI provider", menu=self.ai_menu)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

    # ---------- AI provider link ----------
    def provider_name(self) -> str:
        return providers.SHORT.get(self.provider, "Claude")

    def fill_ai_menu(self) -> None:
        """Rebuilt each time it opens, so it shows the current provider's options."""
        m = self.ai_menu
        m.delete(0, "end")
        for key, label in providers.PROVIDERS.items():
            m.add_radiobutton(label=label, value=key, variable=self.provider_var,
                              command=lambda k=key: self.set_provider(k))
        m.add_separator()
        info = self.account or {}
        m.add_command(label=f"Status: {self.link_line(info)}", state="disabled")
        if self.provider == "groq":
            for name in providers.GROQ_MODELS:
                m.add_radiobutton(label=f"Model: {name.split('/')[-1]}", value=name,
                                  variable=self.groq_model_var,
                                  command=lambda v=name: self.set_model(v))
        if self.provider in providers.KEY_NAMES:
            m.add_command(label="Set API key…", command=self.ask_key)
            m.add_command(label="Get a key (opens browser)", command=self.open_key_page)
            if info.get("linked"):
                m.add_command(label="Remove key", command=self.remove_key)
        else:
            if not info.get("installed", True):
                m.add_command(label="Install…", command=self.link_install)
            if self.provider == "claude":
                m.add_command(label="Sign in with Claude subscription",
                              command=lambda: self.link_login(False))
                m.add_command(label="Sign in with Console (API credits)",
                              command=lambda: self.link_login(True))
            else:
                m.add_command(label="Sign in…", command=lambda: self.link_login(False))
            if info.get("linked") and self.provider in ("claude", "codex"):
                m.add_command(label="Sign out", command=self.link_logout)
        m.add_command(label="Check again", command=self.check_link)

    def link_line(self, info: dict) -> str:
        if self.account is None:
            return "checking…"
        if not info.get("installed", True):
            return "not installed"
        if not info.get("linked"):
            return "not linked"
        if self.provider == "claude":
            return account.describe(info)
        return info.get("who") or "linked"

    def provider_status(self, provider: str) -> dict:
        if provider == "claude":
            return account.status()
        return providers.status(provider)

    def linked(self) -> bool:
        """True when requests can run; otherwise shows the link panel.

        Before the first check finishes, requests go ahead (a missing login
        shows up as an error, which also opens the panel).
        """
        if self.account is None or self.account.get("linked"):
            return True
        self.show_link_panel()
        return False

    def check_link(self, quiet: bool = False) -> None:
        provider = self.provider

        def work() -> None:
            info = self.provider_status(provider)
            self.post(lambda: self.link_checked(provider, info, quiet))
        threading.Thread(target=work, daemon=True).start()

    def link_checked(self, provider: str, info: dict, quiet: bool) -> None:
        if provider != self.provider:  # switched while checking
            return
        was = self.account
        self.account = info
        if provider == "claude" and info.get("claude"):
            self.claude = info["claude"]
        if info["linked"]:
            self.link_polling = False
            if was is not None and not was.get("linked"):  # just signed in
                self.cc_show(f"✅ **Linked to {providers.PROVIDERS[provider]}** "
                             f"({self.link_line(info)}).\n\n"
                             "Requests now use this account's plan or credits.",
                             [], shake=False, auto_hide=8, tag="AI provider")
            return
        if not quiet:
            self.show_link_panel()
        if not self.link_polling and provider not in providers.KEY_NAMES:
            self.link_polling = True  # notice the sign-in finishing by itself
            self.root.after(5000, self.link_poll)

    def link_poll(self) -> None:
        if self.link_polling and not (self.account or {}).get("linked"):
            self.check_link(quiet=True)
            self.root.after(5000, self.link_poll)

    def show_link_panel(self) -> None:
        info = self.account or {}
        name = providers.PROVIDERS[self.provider]
        intro = (f"👋 **Link {name} first**\n\n"
                 "The pet runs on your own account or key, so it uses your "
                 "plan or credits.\n\n")
        if self.provider in providers.KEY_NAMES:
            md = intro + "Paste your API key. It is saved only in the pet's `.env` file."
            actions = [("Enter API key", self.ask_key),
                       ("Get a key", self.open_key_page)]
        elif not info.get("installed", True):
            md = intro + (f"1. **Install {name}** (opens a terminal)\n"
                          "2. Press **Check again**, then sign in.")
            actions = [("Install", self.link_install), ("Check again", self.check_link)]
        elif self.provider == "claude":
            md = intro + ("Sign in once in the terminal that opens. "
                          "The pet notices when you are done.")
            actions = [("Sign in (Claude plan)", lambda: self.link_login(False)),
                       ("Sign in (API credits)", lambda: self.link_login(True))]
        else:
            md = intro + ("Sign in once in the terminal that opens. "
                          "The pet notices when you are done.")
            actions = [("Sign in", lambda: self.link_login(False))]
        actions.append(("Use a different AI", self.show_provider_picker))
        self.cc_show(md, actions, shake=False, auto_hide=None, tag="AI provider",
                     stack=True)

    def show_provider_picker(self) -> None:
        actions = [(("● " if k == self.provider else "") + label,
                    lambda k=k: self.set_provider(k))
                   for k, label in providers.PROVIDERS.items()]
        self.cc_show("**Which AI should the pet use?**\n\n"
                     "Each one runs on your own plan or API key.",
                     actions, shake=False, auto_hide=None, tag="AI provider",
                     stack=True)

    def set_provider(self, key: str) -> None:
        self.provider_var.set(key)
        if key == self.provider and (self.account or {}).get("linked"):
            return
        self.provider = key
        self.state["provider"] = key
        self.session_id = None  # chats don't carry over between providers
        self.state.pop("session_id", None)
        save_state(self.state)
        self.account = None
        self.link_polling = False
        self.placeholder.configure(text=f"Ask {self.provider_name()}…")
        self.model_btn.configure(text=self.model_label())
        self.status(f"Now using {providers.PROVIDERS[key]}.", "info")
        self.check_link()

    def link_install(self) -> None:
        if self.provider == "claude":
            account.install()
        else:
            providers.install(self.provider)
        self.status("Installing in a new terminal…", "info")

    def link_login(self, console: bool) -> None:
        if self.provider == "claude":
            installed = (self.account or {}).get("installed")
            account.login(self.claude if installed else None, console)
        else:
            providers.login(self.provider)
        self.status("Finish signing in in the terminal / browser…", "info")
        if not self.link_polling:
            self.link_polling = True
            self.root.after(5000, self.link_poll)

    def link_logout(self) -> None:
        provider = self.provider

        def work() -> None:
            try:
                if provider == "claude":
                    account.logout(self.claude)
                else:
                    providers.logout(provider)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.post(self.check_link)
        threading.Thread(target=work, daemon=True).start()

    def open_key_page(self) -> None:
        webbrowser.open(providers.KEY_PAGES[self.provider])

    def ask_key(self) -> None:
        provider = self.provider
        name = providers.PROVIDERS[provider]
        key = simpledialog.askstring(name, f"Paste your {name.replace(' API key', '')} "
                                     "API key\n(saved only in the pet's .env file):",
                                     show="•", parent=self.root)
        key = (key or "").strip()
        if not key:
            return
        self.status("Checking the key…", "info")

        def work() -> None:
            ok, detail = providers.check_key(provider, key)
            self.post(lambda: self.key_checked(provider, key, ok, detail))
        threading.Thread(target=work, daemon=True).start()

    def key_checked(self, provider: str, key: str, ok: bool, detail: str) -> None:
        if not ok:
            self.cc_show(f"❌ **That key didn't work.**\n\n{detail}",
                         [("Try again", self.ask_key), ("Get a key", self.open_key_page)],
                         shake=True, auto_hide=None, tag="AI provider")
            return
        providers.set_key(provider, key)
        if provider == "gemini" and detail:
            self.state["gemini_model"] = detail
            save_state(self.state)
        self.account = {"installed": True, "linked": False}  # so the next check greets
        self.check_link()

    def remove_key(self) -> None:
        providers.set_key(self.provider, "")
        self.check_link()

    def effort_menu(self) -> tk.Menu:
        menu = tk.Menu(self.root, tearoff=0)
        for level in EFFORTS:
            menu.add_radiobutton(label=level, value=level,
                                 variable=self.effort_var,
                                 command=self.set_effort)
        return menu

    def show_menu(self, event: tk.Event) -> None:
        self.fx.hide(self.hover)
        self.menu.entryconfigure(
            self.focus_menu_index,
            label="Stop focus timer" if self.focus else self.focus_menu_label())
        self.menu.tk_popup(event.x_root, event.y_root)

    def set_effort(self) -> None:
        self.effort = self.effort_var.get()
        self.state["effort"] = self.effort
        save_state(self.state)
        self.effort_btn.configure(text=self.effort)

    # ---------- model picker (like effort) ----------
    def model_choices(self) -> list[tuple[str, str]]:
        """(value, label) models the current provider can switch between.
        Codex, Antigravity and the Gemini API pick theirs automatically."""
        if self.provider == "claude":
            return [(m, m.capitalize()) for m in CLAUDE_MODELS]
        if self.provider == "groq":
            return [(m, m.split("/")[-1]) for m in providers.GROQ_MODELS]
        return [("auto", "auto")]

    def current_model(self) -> str:
        if self.provider == "claude":
            return self.claude_model
        if self.provider == "groq":
            return self.groq_model_var.get()
        return "auto"

    def model_label(self) -> str:
        return dict(self.model_choices()).get(self.current_model(), self.current_model())

    def set_model(self, value: str) -> None:
        if self.provider == "claude" and value in CLAUDE_MODELS:
            self.claude_model = value
            self.state["claude_model"] = value
            save_state(self.state)
        elif self.provider == "groq" and value in providers.GROQ_MODELS:
            self.groq_model_var.set(value)
            self.save_flag("groq_model", self.groq_model_var)
        self.model_btn.configure(text=self.model_label())

    def fill_model_menu(self) -> None:
        """Right-click > Model, rebuilt for the current provider."""
        m = self.model_menu
        m.delete(0, "end")
        current = self.current_model()
        for value, label in self.model_choices():
            m.add_command(label=("✓ " if value == current else "    ") + label,
                          command=lambda v=value: self.set_model(v))

    def open_model_drop(self, anchor: tk.Widget) -> None:
        current = self.current_model()
        self.open_drop(anchor, [("", label, "✓" if value == current else "",
                                 lambda v=value: self.set_model(v))
                                for value, label in self.model_choices()])

    def on_character(self) -> None:
        self.state["character"] = self.character_var.get()
        save_state(self.state)
        self.happy_until = time.time() + 1.5  # greet with the happy pose
        self.start_hop(0.45, 6 * ZOOM)

    def on_size(self) -> None:
        """New size: save it and restart the pet, since every window is sized from it."""
        size = self.size_var.get()
        if size == PET_SIZE:
            return
        self.state["size"] = size
        # keep the feet where they were: a bigger pet grows upward
        old_h = PET_H
        new_zoom = max(2, round(3 * DPI_SCALE * PET_SIZES[size]))
        new_h = (5 + SIZE + 2) * new_zoom
        self.state["x"] = self.root.winfo_x()
        self.state["y"] = self.root.winfo_y() + old_h - new_h
        save_state(self.state)
        exe = Path(sys.executable)
        gui = exe.with_name("pythonw.exe")
        subprocess.Popen([str(gui if gui.exists() else exe), str(Path(__file__).resolve())],
                         cwd=str(APP_DIR), creationflags=CREATE_NO_WINDOW)
        self.quit()

    def on_walk_toggle(self) -> None:
        self.save_flag("walk", self.walk_var)
        if not self.walk_var.get():
            self.end_walk()

    def on_gravity_toggle(self) -> None:
        self.save_flag("gravity", self.gravity_var)
        if self.gravity_var.get():
            self.drop_to_floor()
        elif self.walk and self.walk_state() == "fall":
            self.end_walk()  # stop mid-air, right where it is

    def on_rim_toggle(self) -> None:
        self.save_flag("rim", self.rim_var)
        self.sprites.set_rim(RIM if self.rim_var.get() else None)

    def save_flag(self, key: str, var: tk.Variable) -> None:
        self.state[key] = var.get()
        save_state(self.state)
        if key == "remind_minutes":
            self.last_reminder = time.time()

    def on_computer_mode(self) -> None:
        self.computer_mode = self.computer_var.get()
        self.state["computer_mode"] = self.computer_mode
        save_state(self.state)

    def confirm_action(self, summary: str) -> bool | str:
        """Ask in the bubble before a risky action ("ask" mode). Any thread."""
        answer: list[bool | str] = [False]
        done = threading.Event()

        def pick(value: bool | str) -> None:
            answer[0] = value
            done.set()
            if self.busy:
                self.root.after(50, lambda: self.show_thinking(self.thinking_prompt))

        def show() -> None:
            self.set_bubble(f"Can I do this?\n{summary}", actions=[
                ("Allow", lambda: pick(True)), ("Always", lambda: pick("always")),
                ("Deny", lambda: pick(False))])
            self.tool_tag.configure(text="Computer")
            self.show_bubble()
            self.start_hop(0.42, 6 * ZOOM)

        self.post(show)
        done.wait(120)  # no answer in 2 minutes: declined
        return answer[0]

    def pet_tool(self, name: str, args: dict) -> str:
        """Reminders and to-dos asked for by the AI (runs on the Tk thread)."""
        out: list[str] = ["Done."]
        done = threading.Event()
        text = str(args.get("text", "")).strip()

        def run() -> None:
            try:
                if args.get("action") == "task":
                    task = self.tasks.add(text)
                    self.render_tasks()
                    out[0] = f"Added the task “{task['text']}”." if task else "Empty task."
                    return
                delay = float(args.get("minutes") or 0) * 60
                if args.get("at"):
                    hh, mm = map(int, str(args["at"]).split(":"))
                    now = time.localtime()
                    delay = (hh * 3600 + mm * 60) - (now.tm_hour * 3600 + now.tm_min * 60
                                                     + now.tm_sec)
                    if delay < 0:
                        delay += 24 * 3600
                delay = max(1.0, delay)
                self.root.after(int(delay * 1000), lambda: self.remind(
                    f"⏰ **Reminder:** {text}", speak=text, shake=True))
                out[0] = f"Reminder set for {time.strftime('%H:%M', time.localtime(time.time() + delay))}."
            except (ValueError, tk.TclError) as exc:
                out[0] = f"Couldn't set it: {exc}"
            finally:
                done.set()

        self.post(run)
        done.wait(10)
        return out[0]

    def mcp_config(self) -> str:
        """--mcp-config JSON that points Claude Code at pet_mcp.py and the bridge."""
        port, token = self.bridge
        return json.dumps({"mcpServers": {"pc": {
            "type": "stdio", "command": sys.executable,
            "args": [str(APP_DIR / "pet_mcp.py")],
            "env": {"CLAUDE_PET_BRIDGE": f"{port}:{token}"}}}})

    def on_tools_toggle(self) -> None:
        self.state["always_tools"] = self.tools_var.get()
        save_state(self.state)

    def on_speak_toggle(self) -> None:
        self.state["speak"] = self.speak_var.get()
        save_state(self.state)
        if not self.speak_var.get():
            self.stop_speaking()

    def copy_reply(self) -> None:
        if self.last_reply:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.last_reply)
            self.flash_copy()

    # ---------- pet motion ----------
    def start_hop(self, duration: float, height: float) -> None:
        self.hop = (time.perf_counter(), duration, height)

    def start_shake(self, duration: float = 0.45) -> None:
        self.shake = (time.perf_counter(), duration)

    def pose(self, now: float) -> dict:
        """Pick the sprite pose for the current state."""
        f = int((now - self.t0) * SPRITE_FPS)
        tail = (0, 1, 2, 1)[(f // 6) % 4]
        if self.drag_start and self.moved:  # picked up: startled
            return dict(eyes="wide", tail=(f // 3) % 4)
        if self.drop_hover:  # something is being dragged over: ears up
            return dict(eyes="wide", tail=(f // 3) % 4, hat_lift=1)
        if self.listening:
            return dict(eyes="wide", tail=tail, sound=1 + (f // 6) % 2)
        if self.busy and self.stream_view:  # answer is streaming in
            return dict(eyes=self.gaze(now), tail=tail,
                        mouth="open" if (f // 3) % 2 else "closed")
        if self.busy:
            return dict(eyes="right", tail=(f // 4) % 4, busy=(f // 6) % 4)
        if self.is_speaking():
            return dict(eyes=self.gaze(now), tail=tail,
                        mouth="open" if (f // 3) % 2 else "closed")
        if self.cc_waiting:  # Claude Code needs permission: wave, "!"
            return dict(eyes=("wide", "up")[(f // 8) % 2], tail=(f // 3) % 4,
                        hat_lift=(f // 4) % 2, mark="alert", mark_f=(f // 5) % 2)
        if time.time() < self.worried_until:
            return dict(eyes=("down_left", "down_right")[(f // 12) % 2], tail=0,
                        mark="sweat")
        if time.time() < self.happy_until:
            if time.time() < self.sparkle_until:
                return dict(eyes="happy", tail=tail, hat_lift=1, mark="sparkle",
                            mark_f=(f // 4) % 2)
            return dict(eyes="happy", tail=tail, hat_lift=1)
        if self.walk:
            state = self.walk_state()
            side = "left" if self.walk["dir"] < 0 else "right"  # faces where it goes
            if state == "fall":
                return dict(eyes="wide", tail=(f // 3) % 4, facing=side)
            if state == "move":
                return dict(eyes=side, tail=(f // 3) % 4, facing=side)
            # pausing: looks around, still turned the way it was heading
            return dict(eyes=self.gaze(now), tail=tail, facing=side)
        if self.cc_running:  # a Claude Code session is working: pondering
            return dict(eyes=("up_right", "up", "up_left", "up")[(f // 25) % 4],
                        tail=tail, mark="think", mark_f=(f // 8) % 4)
        if self.sleeping():
            return dict(eyes="sleep", tail=0, zzz=(f // 10) % 3)
        if now < self.blink_until:
            return dict(eyes="blink", tail=tail)
        return dict(eyes=self.gaze(now), tail=tail,
                    hat_lift=int(now < self.hat_tip_until))

    def gaze(self, now: float) -> str:
        """Eye direction toward the mouse pointer (8 ways, or "open").

        Looks straight ahead when the pointer is on the face or has been
        still for a few seconds, so the pet doesn't stare forever.
        """
        try:
            px, py = self.root.winfo_pointerxy()
        except tk.TclError:
            return "open"
        if (px, py) != self.pointer:
            self.pointer, self.pointer_moved = (px, py), now
        want = "open"
        if now - self.pointer_moved < GAZE_HOLD_SECONDS:
            hx = self.root.winfo_rootx() + SWAY_ROOM + SIZE * ZOOM / 2
            hy = self.root.winfo_rooty() + HOP_ROOM + 13 * ZOOM
            dx, dy = px - hx, py - hy
            dist = math.hypot(dx, dy)
            if dist > GAZE_DEADZONE * ZOOM:
                lean = dist * 0.38  # sin(22.5 deg): splits the circle in 8
                vert = "up" if dy < -lean else "down" if dy > lean else ""
                side = "left" if dx < -lean else "right" if dx > lean else ""
                want = "_".join(n for n in (vert, side) if n)
        if want != self.gaze_next[0]:
            self.gaze_next = (want, now)
        elif want != self.gaze_now and now - self.gaze_next[1] > 0.06:
            self.gaze_now = want
        return self.gaze_now

    def sleeping(self) -> bool:
        return (not self.busy and not self.listening and not self.cc_running
                and not self.cc_waiting
                and time.time() - self.last_active > SLEEP_AFTER_SECONDS)

    def offsets(self, now: float) -> tuple[float, float, float]:
        """(x, y, lift) in screen px: breathing, hops, shake and drag sway."""
        t = now - self.t0
        if self.busy:
            bob = 0.6 * ZOOM * (0.5 - 0.5 * math.cos(t * 2 * math.pi / 0.7))
        elif self.walk:
            bob = 0.0
        elif self.sleeping():
            bob = 1.0 * ZOOM * (0.5 - 0.5 * math.cos(t * 2 * math.pi / 4.0))
        elif self.is_speaking():
            bob = 0.5 * ZOOM * abs(math.sin(t * 9))
        else:
            bob = 0.5 * ZOOM * (0.5 - 0.5 * math.cos(t * 2 * math.pi / 2.6))
        lift = 0.0
        if self.hop:
            start, dur, height = self.hop
            p = (now - start) / dur
            if p >= 1:
                self.hop = None
            else:
                lift = height * 4 * p * (1 - p)
        if self.walk and self.walk_state() == "move":
            lift += 1.3 * ZOOM * abs(math.sin(t * math.pi * WALK_STEP_HZ))
        x = self.sway
        if self.shake:
            start, dur = self.shake
            p = (now - start) / dur
            if p >= 1:
                self.shake = None
            else:
                x += math.sin(p * 30) * 1.2 * ZOOM * (1 - p)
        return x, bob, lift

    def draw_pet(self, now: float) -> None:
        character = self.character_var.get()
        image = self.sprites.get(character=character, **self.pose(now))
        dx, bob, lift = self.offsets(now)
        x = round(SWAY_ROOM + dx)
        y = round(HOP_ROOM + bob - lift)
        if character not in self.feet_cache:
            self.feet_cache[character] = feet_of(character)
        feet, c0, c1 = self.feet_cache[character]
        # shadow shrinks while the pet is in the air
        shrink = 1 - min(lift / (6 * ZOOM), 1) * 0.45
        half = (c1 - c0 + 1) * ZOOM * 0.42 * shrink
        cx = SWAY_ROOM + (c0 + c1 + 1) * ZOOM / 2
        sy = HOP_ROOM + (feet + 1) * ZOOM
        shadow = (round(cx - half), round(sy - ZOOM * 0.6 * shrink),
                  round(cx + half), round(sy + ZOOM * 0.8 * shrink))
        badge = None
        if self.unread and not self.busy:
            r = 7 + 1.2 * math.sin((now - self.t0) * 5)
            bx, by = PET_W - 12, HOP_ROOM + 4
            badge = (round(bx - r), round(by - r), round(bx + r), round(by + r))
        state = (id(image), x, y, shadow, badge)
        if state == self.last_drawn:
            return
        self.last_drawn = state
        c = self.canvas
        c.itemconfigure(self.sprite_item, image=image)
        c.coords(self.sprite_item, x, y)
        c.coords(self.shadow_item, *shadow)
        if badge:
            c.coords(self.badge_item, *badge)
            c.coords(self.badge_text, (badge[0] + badge[2]) / 2,
                     (badge[1] + badge[3]) / 2)
            c.itemconfigure(self.badge_item, state="normal")
            c.itemconfigure(self.badge_text, state="normal")
        else:
            c.itemconfigure(self.badge_item, state="hidden")
            c.itemconfigure(self.badge_text, state="hidden")

    def tick(self) -> None:
        """One animation frame: run queued events, then draw."""
        try:
            while not self.events.empty():
                try:
                    self.events.get_nowait()()
                except Exception:  # noqa: BLE001 - the other queued events still run
                    pass
            now = time.perf_counter()
            if now > self.next_blink:
                self.blink_until = now + 0.14
                self.next_blink = now + random.uniform(2.5, 6)
                if random.random() < 0.25:  # sometimes a double blink
                    self.next_blink = now + 0.35
            if now > self.hat_tip_until and random.random() < 0.0004:
                self.hat_tip_until = now + 0.6
            # drag sway: spring toward the target, target eases back to rest
            if self.cc_waiting and not self.drag_start and not self.walk:
                self.sway_target = math.sin((now - self.t0) * 9) * 1.6 * ZOOM  # waving
                if now > self.next_wave_hop:
                    self.next_wave_hop = now + WAVE_HOP_SECONDS
                    self.start_hop(0.36, 5 * ZOOM)
            self.sway += (self.sway_target - self.sway) * 0.25
            self.sway_target *= 0.85
            if self.wake:  # the wake word rests while the mic or voice is busy
                self.wake.pause(self.listening or self.finishing or self.is_speaking()
                                or self.wake_session or self.cont is not None)
            self.update_walk(now)
            self.draw_pet(now)
            self.update_ui(now)
        except Exception:  # noqa: BLE001 - one bad frame must never stop the pet
            pass
        finally:
            self.root.after(self.frame_ms(), self.tick)

    def frame_ms(self) -> int:
        """60 fps while things move, less when calm, to save CPU."""
        active = (self.hop or self.shake or self.drag_start or self.busy or self.walk
                  or self.cc_waiting
                  or (self.hack_bars and self.fx.shown(self.hacks_win))
                  or self.listening or abs(self.sway) > 0.05 or self.fx.jobs
                  or self.reveal_shown < self.reveal_total or self.is_speaking()
                  or self.ring.particles or self.glow.animating())
        fast_timer(bool(active))
        if active:
            return TICK_MS
        # Calm: breathing and eye moves are slow, a few frames a second do.
        return IDLE_SLEEP_MS if self.sleeping() else IDLE_TICK_MS

    def update_ui(self, now: float) -> None:
        """Per-frame UI touches: mic pulse, thinking timer, text reveal."""
        if self.listening:
            p = 0.5 + 0.5 * math.sin((now - self.t0) * 6)
            self.mic_btn.configure(fg=lerp_color(RED, RED_SOFT, p))
        if self.busy:
            self.update_thinking(now)
            self.draw_stream(now)
            if self.stream_view:
                on = int((now - self.t0) * 2.2) % 2 == 0
                self.bubble_text.tag_configure(
                    "stream_caret", foreground=ACCENT if on else PANEL_BG)
        if self.hack_bars and self.fx.shown(self.hacks_win):
            self.shimmer_hacks(now)
        self.update_focus(now)
        if self.reveal_shown < self.reveal_total:
            p = min((now - self.reveal_start) / self.reveal_dur, 1.0)
            shown = max(self.reveal_shown, round(self.reveal_total * (1 - (1 - p) ** 2)))
            if shown != self.reveal_shown:
                self.reveal_shown = shown
                self.bubble_text.tag_remove("hidden", "1.0", f"1.0 + {shown} chars")
                self.fit_bubble()

    def keep_on_top(self) -> None:
        # Some full-screen apps steal topmost; re-assert it periodically.
        # Order matters: glow at the bottom, ring above it, pet on top.
        self.glow.raise_()
        if self.ring.win.winfo_viewable():
            self.ring.win.attributes("-topmost", True)
        for win in (self.root, self.bar, self.bubble, self.history, self.hover,
                    self.tasks_win, self.hacks_win):
            if win.winfo_viewable():
                win.attributes("-topmost", True)
        self.root.after(3000, self.keep_on_top)

    # ---------- taskbar strolls ----------
    def floor_y(self) -> int:
        """Window y that stands the pet's feet on top of the taskbar."""
        character = self.character_var.get()
        if character not in self.feet_cache:
            self.feet_cache[character] = feet_of(character)
        feet = self.feet_cache[character][0]
        return work_area()[3] - HOP_ROOM - (feet + 1) * ZOOM - ZOOM

    def can_walk(self) -> bool:
        """Only stroll when nothing needs the pet and no panel is open."""
        if (not self.walk_var.get() or self.busy or self.listening
                or self.is_speaking() or self.drag_start or self.drop_hover
                or self.snipping or self.briefing or self.focus or self.cc_waiting):
            return False
        return not any(self.fx.shown(w) for w in (
            self.bar, self.bubble, self.history, self.hover, self.tasks_win,
            self.hacks_win))

    def walk_state(self) -> str:
        """"fall" (dropping to the taskbar), "move" or "pause"."""
        w = self.walk
        if not w:
            return ""
        if w["vy"] or w["y"] < w["floor"] - 0.5:
            return "fall"
        if w["legs"] and time.perf_counter() >= w["pause_until"]:
            return "move"
        return "pause"

    def maybe_start_walk(self) -> None:
        if time.time() < self.next_walk:
            return
        self.next_walk = time.time() + random.uniform(*WALK_EVERY)
        if not self.can_walk() or idle_seconds() > WALK_USER_IDLE_MAX:
            return
        left, _top, right, _bottom = work_area()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        lo, hi = left, right - PET_W
        if not lo <= x <= hi:
            return  # parked on another monitor: stay put
        # Cross the whole taskbar to the far edge, stopping to look around
        # once or twice on the way; the next stroll heads back.
        end = lo if x - lo > hi - x else hi
        stops = sorted(random.uniform(0.2, 0.8) for _ in range(random.randint(1, 2)))
        legs = [x + (end - x) * f for f in stops] + [float(end)]
        now = time.perf_counter()
        # gravity off: stroll along the height it was left at
        floor = self.floor_y() if self.gravity_var.get() else y
        # a pet parked higher up hops off and drops to the taskbar first
        self.walk = {"x": float(x), "y": float(y), "floor": floor,
                     "vy": -320.0 * DPI_SCALE if y < floor - 1 else 0.0,
                     "legs": legs, "dir": 1 if end >= x else -1, "t": now,
                     "pause_until": now + 0.6}

    def update_walk(self, now: float) -> None:
        w = self.walk
        if not w:
            self.maybe_start_walk()
            return
        if not w.get("drop") and not self.can_walk():
            self.end_walk()
            return
        dt = min(now - w["t"], 0.1)
        w["t"] = now
        state = self.walk_state()
        if state == "fall":
            w["vy"] += WALK_GRAVITY * dt
            w["y"] = min(w["y"] + w["vy"] * dt, w["floor"])
            if w["y"] >= w["floor"] and w["vy"] > 0:
                w["vy"] = 0.0
                w["pause_until"] = now + 0.7
                self.start_hop(0.25, 2 * ZOOM)  # landing bounce
        elif w["y"] > w["floor"]:
            w["y"] = float(w["floor"])  # pet sat lower than the taskbar top
        elif state == "move":
            target = w["legs"][0]
            gap = target - w["x"]
            w["dir"] = 1 if gap > 0 else -1
            step = WALK_SPEED * dt
            if abs(gap) <= step:
                w["x"] = target
                w["legs"].pop(0)
                w["pause_until"] = now + random.uniform(0.8, 2.6)
            else:
                w["x"] += w["dir"] * step
        elif not w["legs"] and now >= w["pause_until"]:
            self.end_walk()
            return
        self.root.geometry(f"+{round(w['x'])}+{round(w['y'])}")
        if w.get("drop"):  # open panels follow the falling pet
            self.place_ring()
            if any(self.fx.shown(win) for win in (self.bar, self.bubble, self.history)):
                self.place_popups()

    def drop_to_floor(self) -> None:
        """Gravity: fall from wherever the pet is down onto the taskbar."""
        if not self.gravity_var.get() or self.walk or self.drag_start:
            return
        left, _top, right, _bottom = work_area()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if not left <= x <= right - PET_W:
            return  # parked on another monitor: leave it there
        floor = self.floor_y()
        if abs(y - floor) <= 1:
            return
        now = time.perf_counter()
        self.walk = {"x": float(x), "y": float(y), "floor": floor, "vy": 0.0,
                     "legs": [], "dir": 1, "t": now, "pause_until": now,
                     "drop": True}

    def end_walk(self) -> None:
        """Stop where the pet stands and remember the spot."""
        if not self.walk:
            return
        self.walk = None
        self.state["x"], self.state["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_state(self.state)

    # ---------- dragging ----------
    def on_press(self, event: tk.Event) -> None:
        self.last_active = time.time()
        self.end_walk()
        self.drag_start = (event.x_root - self.root.winfo_x(),
                           event.y_root - self.root.winfo_y())
        self.drag_last = (event.x_root, time.perf_counter())
        self.moved = False

    def on_drag(self, event: tk.Event) -> None:
        if not self.drag_start:
            return
        x = event.x_root - self.drag_start[0]
        y = event.y_root - self.drag_start[1]
        if not self.moved and abs(x - self.root.winfo_x()) + abs(y - self.root.winfo_y()) < 3:
            return  # ignore tiny jitter so clicks stay clicks
        lx, lt = self.drag_last
        now = time.perf_counter()
        velocity = (event.x_root - lx) / max(now - lt, 1e-3)
        self.drag_last = (event.x_root, now)
        # body lags behind the motion, like it's being carried
        self.sway_target = max(-SWAY_ROOM, min(SWAY_ROOM, -velocity * 0.012))
        self.root.geometry(f"+{x}+{y}")
        self.place_ring()
        self.moved = True
        self.fx.hide(self.hover)
        self.place_popups()

    def on_release(self, _event: tk.Event) -> None:
        if self.moved:
            self.state["x"], self.state["y"] = self.root.winfo_x(), self.root.winfo_y()
            save_state(self.state)
            if not self.gravity_var.get():
                self.start_hop(0.28, 2.5 * ZOOM)  # little landing bounce
        elif self.cc_waiting:  # waving about a permission prompt: take me there
            sid, wait = max(self.cc_waiting.items(), key=lambda kv: kv[1]["since"])
            self.cc_waiting.pop(sid, None)
            self.cc_focus(wait["event"])
        else:
            self.start_hop(0.22, 2 * ZOOM)
            self.toggle_bar()
        self.drag_start = None
        if self.moved:
            self.drop_to_floor()

    # ---------- layout ----------
    def place_popups(self) -> None:
        """Ask box sits centered below the pet; bubble and history above it.

        If the box would run off the bottom of the screen (pet parked near
        the taskbar), it flips above the pet and the bubble stacks above it.
        """
        left, top, right, bottom = work_area()
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        cx = rx + PET_W // 2
        sprite_top = ry + HOP_ROOM

        def clamp(x: int, y: int, w: int, h: int) -> tuple[int, int]:
            return min(max(x, left), right - w), min(max(y, top), bottom - h)

        def size(win: tk.Toplevel) -> tuple[int, int]:
            win.update_idletasks()
            return win.winfo_reqwidth(), win.winfo_reqheight()

        bw, bh = size(self.bar)
        # Decide using the fully grown height so the box never jumps sides.
        line_px = tkfont.Font(font=self.font).metrics("linespace")
        max_bh = self.bar_line_h + (BAR_MAX_LINES - 1) * line_px
        self.bar_below = ry + PET_H + 4 + max_bh <= bottom
        by = ry + PET_H + 4 if self.bar_below else sprite_top - bh - 6
        self.fx.place(self.bar, *clamp(cx - bw // 2, by, bw, bh))

        anchor_y = sprite_top + 6 if self.bar_below else by - 6
        for win in (self.bubble, self.history, self.tasks_win, self.hacks_win):
            w, h = size(win)
            y = anchor_y - h if anchor_y - h > top else ry + PET_H + bh + 10
            self.fx.place(win, *clamp(cx - w // 2, y, w, h))

        hw, hh = size(self.hover)
        hy = ry + PET_H + 2 if ry + PET_H + 2 + hh <= bottom else sprite_top - hh - 4
        self.fx.place(self.hover, *clamp(cx - hw // 2, hy, hw, hh))
        gw, gh = size(self.hack_badge)
        gy = ry + PET_H + 2 if ry + PET_H + 2 + gh <= bottom else sprite_top - gh - 4
        self.fx.place(self.hack_badge, *clamp(cx - gw // 2, gy, gw, gh))

    # ---------- ask box ----------
    def build_bar(self) -> None:
        self.bar, inner = self.popup()
        self.logo_btn = tk.Label(inner, text="✱", bg=PANEL_BG, fg=ACCENT,
                                 font=(self.font[0], 13, "bold"), cursor="hand2",
                                 padx=3)
        self.logo_btn.pack(side="left", anchor="n", padx=(7, 1), pady=4)
        self.logo_btn.bind("<Enter>", lambda _e: self.logo_btn.configure(
            bg=HOVER_BG, fg=ACCENT_HOVER))
        self.logo_btn.bind("<Leave>", lambda _e: self.logo_btn.configure(
            bg=PANEL_BG, fg=ACCENT))
        self.logo_btn.bind("<Button-1>", lambda _e: self.toggle_quick_menu())
        self.mic_btn = self.icon_button(inner, "🎤", self.toggle_listen,
                                        ("Segoe UI Emoji", 11), side="right",
                                        anchor="n", padx=(0, 6), pady=4)
        self.send_btn = self.icon_button(inner, "➤", self.send_clicked,
                                         (self.font[0], 11), fg=PANEL_BG,
                                         hover_fg=ACCENT, side="right",
                                         anchor="n", pady=4)
        self.effort_btn = tk.Label(inner, text=self.effort, bg=INPUT_BG, fg=MUTED,
                                   font=self.font_small, cursor="hand2",
                                   padx=6, pady=1)
        self.effort_btn.pack(side="right", anchor="n", padx=4, pady=9)
        self.effort_btn.bind("<Button-1>", lambda _e: self.open_effort_drop(
            self.effort_btn))
        self.model_btn = tk.Label(inner, text=self.model_label(), bg=INPUT_BG, fg=MUTED,
                                  font=self.font_small, cursor="hand2",
                                  padx=6, pady=1)
        self.model_btn.pack(side="right", anchor="n", padx=(4, 0), pady=9)
        self.model_btn.bind("<Button-1>", lambda _e: self.open_model_drop(
            self.model_btn))

        holder = tk.Frame(inner, bg=PANEL_BG)
        holder.pack(side="left", fill="x", expand=True, pady=8)
        self.entry = tk.Text(holder, width=BAR_CHARS, height=1, wrap="word",
                             bg=PANEL_BG, fg=PANEL_FG, relief="flat",
                             font=self.font, **CARET, padx=0, pady=0,
                             selectbackground=ACCENT_DARK, highlightthickness=0)
        self.entry.pack(fill="x")
        self.placeholder = tk.Label(holder, text=f"Ask {self.provider_name()}…", bg=PANEL_BG,
                                    fg=MUTED, font=self.font)
        self.placeholder.place(x=CARET_GAP, y=0)
        self.placeholder.bind("<Button-1>", lambda _e: self.entry.focus_force())

        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<KeyRelease>", lambda _e: self.update_placeholder())
        # Re-measure once the box has its real width (wrapping depends on it).
        self.entry.bind("<Configure>", lambda _e: self.update_placeholder())
        self.entry.bind("<FocusIn>", lambda _e: set_border(self.bar, ACCENT))
        self.entry.bind("<FocusOut>", lambda _e: set_border(self.bar, BORDER))
        self.bar.update_idletasks()
        self.bar_line_h = self.bar.winfo_reqheight()
        self.bar_below = True
        self.entry.bind("<Escape>", lambda _e: self.hide_all())
        self.entry.bind("<Control-m>", lambda _e: self.toggle_listen() or "break")

    def focus_input(self, win: tk.Toplevel, widget: tk.Widget) -> None:
        """Give a popup real keyboard focus so its caret shows and blinks.

        Borderless windows are not activated by Windows on their own, so
        bring the window to the foreground first, then focus the field.
        """
        try:
            ctypes.windll.user32.SetForegroundWindow(hwnd_of(win))
        except OSError:
            pass
        widget.focus_force()
        widget.after(60, widget.focus_force)  # once the fade-in has mapped it

    def entry_text(self) -> str:
        return self.entry.get("1.0", "end-1c")

    def set_entry(self, text: str) -> None:
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", text)
        self.entry.mark_set("insert", "end")
        self.update_placeholder()

    def update_placeholder(self) -> None:
        """Show the hint when empty and grow the box to fit wrapped lines."""
        has_text = bool(self.entry_text().strip())
        if self.entry_text():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=CARET_GAP, y=0)
        self.send_btn.configure(fg=ACCENT if has_text else PANEL_BG)
        self.entry.update_idletasks()
        breaks = self.entry.count("1.0", "end-1c", "displaylines")
        height = min((breaks[0] if breaks else 0) + 1, BAR_MAX_LINES)
        if int(self.entry.cget("height")) != height:
            self.entry.configure(height=height)
            if self.fx.shown(self.bar):
                self.place_popups()
        self.entry.see("insert")

    # ---------- themed dropdown (✱ quick menu, effort list) ----------
    def build_drop(self) -> None:
        self.drop, self.drop_inner = self.popup()
        self.drop_anchor: tk.Widget | None = None
        self.drop.bind("<FocusOut>", lambda _e: self.root.after(120, self.drop_focus_check))
        self.drop.bind("<Escape>", lambda _e: self.hide_drop())

    def drop_focus_check(self) -> None:
        """Close the dropdown once focus moves to anything outside it."""
        try:
            focus = self.root.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is None or focus.winfo_toplevel() is not self.drop:
            self.hide_drop()

    def hide_drop(self) -> None:
        self.drop_anchor = None
        self.fx.hide(self.drop)

    def open_drop(self, anchor: tk.Widget, items: list) -> None:
        """Show a dark menu under `anchor`.

        items: (icon, label, hint, action) rows, or None for a divider.
        hint is grey text on the right ("✓" is drawn in the accent colour).
        """
        for child in self.drop_inner.winfo_children():
            child.destroy()
        for item in items:
            if item is None:
                tk.Frame(self.drop_inner, bg=BORDER, height=1).pack(fill="x", padx=10,
                                                                    pady=3)
                continue
            icon, text, hint, action = item
            row = tk.Frame(self.drop_inner, bg=PANEL_BG, cursor="hand2")
            row.pack(fill="x", padx=4)
            parts = [row,
                     tk.Label(row, text=icon, bg=PANEL_BG, fg=ACCENT, width=3,
                              font=("Segoe UI Emoji", 10)),
                     tk.Label(row, text=text, bg=PANEL_BG, fg=PANEL_FG, anchor="w",
                              font=self.font_small, padx=2, pady=5)]
            parts[1].pack(side="left", padx=(4, 0))
            parts[2].pack(side="left", fill="x", expand=True)
            if hint:
                tip = tk.Label(row, text=hint, bg=PANEL_BG, font=self.font_small,
                               fg=ACCENT if hint == "✓" else MUTED, padx=10)
                tip.pack(side="right")
                parts.append(tip)

            def paint(color: str, parts: list = parts) -> None:
                for w in parts:
                    w.configure(bg=color)
            for w in parts:
                w.bind("<Enter>", lambda _e, f=paint: f(HOVER_BG))
                w.bind("<Leave>", lambda _e, f=paint: f(PANEL_BG))
                w.bind("<Button-1>", lambda _e, a=action: (
                    self.hide_drop(), self.root.after(10, a)))
        tk.Frame(self.drop_inner, bg=PANEL_BG, height=4).pack()
        self.drop.update_idletasks()
        w, h = self.drop.winfo_reqwidth(), self.drop.winfo_reqheight()
        left, top, right, bottom = work_area()
        x = anchor.winfo_rootx() - 6
        y = anchor.winfo_rooty() + anchor.winfo_height() + 6
        if y + h > bottom:  # no room below: open upwards
            y = anchor.winfo_rooty() - h - 6
        self.drop_anchor = anchor
        self.fx.place(self.drop, min(max(x, left), right - w), max(y, top))
        self.fx.show(self.drop, slide=-1 if y > anchor.winfo_rooty() else 1)
        self.focus_input(self.drop, self.drop)

    def toggle_quick_menu(self) -> None:
        if self.fx.shown(self.drop) and self.drop_anchor is self.logo_btn:
            self.hide_drop()
            return
        on = lambda var: "✓" if var.get() else ""  # noqa: E731
        u = self.chat.usage
        flip = lambda var, fn: lambda: (var.set(not var.get()), fn())  # noqa: E731
        items = [
            ("⌨", "Ask", "Ctrl+Alt+Space", lambda: self.focus_input(self.bar, self.entry)),
            ("🎤", "Talk", "Ctrl+M", self.toggle_listen),
            ("🎙", "Listen all the time", on(self.cont_var), self.toggle_continuous),
            ("📷", "Screenshot", "", self.snip),
            None,
            ("💬", "New chat", "", self.new_chat),
            ("🗜", "Compact chat",
             f"context {chatlog.short(u['context'])}" if u["context"] else "",
             self.compact_chat),
            ("🕘", "Chat history", "", self.toggle_history),
            ("📋", "Copy last reply", "", self.copy_reply),
            None,
            ("✨", "Model", f"{self.model_label()}  ›",
             lambda: self.open_model_drop(self.logo_btn)),
            ("🧠", "Effort", f"{self.effort}  ›",
             lambda: self.open_effort_drop(self.logo_btn)),
            ("🤖", "AI", f"{self.provider_name()}  ›", self.show_provider_picker),
            ("🔧", "Always use tools", on(self.tools_var),
             flip(self.tools_var, self.on_tools_toggle)),
            ("🔊", "Read replies aloud", on(self.speak_var),
             flip(self.speak_var, self.on_speak_toggle)),
            None,
            ("✓", "Tasks", "", self.toggle_tasks),
            ("📊", "Usage", "", self.show_usage),
            ("⌨", "Keystrokes", keys.short(keys.today_count(self.state)) + " today",
             self.show_keys),
        ]
        self.open_drop(self.logo_btn, items)

    def open_effort_drop(self, anchor: tk.Widget) -> None:
        def pick(level: str) -> None:
            self.effort_var.set(level)
            self.set_effort()
        self.open_drop(anchor, [("", level, "✓" if level == self.effort else "",
                                 lambda lv=level: pick(lv)) for level in EFFORTS])

    def send_clicked(self) -> None:
        if self.entry_text().strip() or self.listening or self.finishing:
            self.on_enter(None)

    # ---------- hover quick actions (Type / Talk) ----------
    def build_hover(self) -> None:
        self.hover, inner = self.popup()
        self.hover_job: str | None = None
        for i, (text, action) in enumerate((("⌨  Type", self.show_bar),
                                            ("🎤  Talk", self.talk),
                                            ("📷  Snip", self.snip),
                                            ("✓  Tasks", self.toggle_tasks),
                                            ("🏆  Hackathons", self.toggle_hacks))):
            if i:
                tk.Frame(inner, bg=BORDER, width=1).pack(side="left", fill="y",
                                                         pady=6)
            btn = tk.Label(inner, text=text, bg=PANEL_BG, fg=PANEL_FG,
                           font=("Segoe UI Emoji", 9), cursor="hand2",
                           padx=12, pady=5)
            btn.pack(side="left")
            btn.bind("<Enter>", lambda _e, b=btn: (b.configure(bg=HOVER_BG),
                                                   self.hover_enter()))
            btn.bind("<Leave>", lambda _e, b=btn: (b.configure(bg=PANEL_BG),
                                                   self.hover_leave()))
            btn.bind("<Button-1>", lambda _e, a=action: (self.fx.hide(self.hover), a()))

    def hover_enter(self) -> None:
        if self.hover_job:
            self.root.after_cancel(self.hover_job)
            self.hover_job = None
        if (self.fx.shown(self.bar) or self.drag_start
                or self.fx.shown(self.hover)):
            return
        self.place_popups()
        self.fx.show(self.hover, slide=-1)
        self.show_usage_ring()

    def hover_leave(self) -> None:
        # Short delay so the pointer can travel from the pet onto the buttons.
        if self.hover_job:
            self.root.after_cancel(self.hover_job)
        self.hover_job = self.root.after(350, self.hover_hide)

    def hover_hide(self) -> None:
        self.hover_job = None
        self.fx.hide(self.hover)
        self.usage_ring.hide()

    def show_bar(self) -> None:
        self.fx.hide(self.hover)
        self.last_active = time.time()
        if preload_whisper:  # you may talk next: warm the mic model meanwhile
            preload_whisper()
        self.place_popups()
        self.fx.show(self.bar, slide=-1 if self.bar_below else 1)
        self.focus_input(self.bar, self.entry)
        if self.unread:
            self.unread = False
            self.show_bubble()

    def toggle_bar(self) -> None:
        if self.fx.shown(self.bar):
            self.hide_all()
        else:
            self.show_bar()

    def hide_all(self) -> None:
        if self.listening or self.finishing:
            self.cancel_listening()
        for win in (self.bar, self.bubble, self.history, self.hover,
                    self.tasks_win, self.drop):
            self.fx.hide(win)

    def on_enter(self, event: tk.Event | None) -> str | None:
        if event is not None and event.state & 0x0001:  # Shift+Enter: new line
            self.root.after(1, self.update_placeholder)
            return None
        if self.listening or self.finishing:
            # Send once the last spoken words are transcribed.
            self.send_pending = True
            self.stop_listening()
            return "break"
        prompt = self.entry_text().strip()
        if self.attachment and not self.busy:
            self.set_entry("")
            self.ask_attachment(prompt)
            return "break"
        if prompt.startswith(("+ ", "/todo ")):  # quick add, no Claude call
            self.set_entry("")
            self.add_task(prompt.split(" ", 1)[1])
            return "break"
        if prompt and not self.busy:
            self.set_entry("")
            self.send(prompt, voice=self.voice_used)
            self.voice_used = False
        return "break"

    # ---------- reply bubble ----------
    def build_bubble(self) -> None:
        self.bubble, inner = self.popup()
        self.bubble_text = tk.Text(inner, wrap="word", bg=PANEL_BG, fg=PANEL_FG,
                                   relief="flat", font=self.font, padx=14,
                                   pady=10, width=BUBBLE_CHARS, height=1,
                                   cursor="arrow", spacing1=1, spacing3=1,
                                   highlightthickness=0,
                                   selectbackground=ACCENT_DARK)
        self.bubble_text.pack(fill="both", expand=True)
        setup_markdown(self.bubble_text, PANEL_FG, MUTED, ACCENT, INPUT_BG,
                       self.font)
        self.bubble_text.tag_configure("info", foreground=MUTED)
        self.bubble_text.tag_configure("error", foreground="#FF8A80")
        self.bubble_text.tag_configure("prompt", foreground=MUTED,
                                       font=(self.font[0], 9, "italic"),
                                       spacing3=6)
        self.bubble_text.tag_configure("think", foreground=ACCENT)
        self.bubble_text.tag_configure("stream_caret", foreground=ACCENT)
        self.action_row = tk.Frame(inner, bg=PANEL_BG)
        self.action_row.pack(fill="x", padx=12)
        footer = tk.Frame(inner, bg=PANEL_BG)
        footer.pack(fill="x", padx=8, pady=(0, 6))
        self.icon_button(footer, "✕", lambda: self.fx.hide(self.bubble),
                         side="right")
        self.icon_button(footer, "History", self.toggle_history, side="right")
        self.copy_btn = self.icon_button(footer, "Copy", self.copy_reply,
                                         side="right")
        self.tool_tag = tk.Label(footer, text="", bg=PANEL_BG, fg=MUTED,
                                 font=self.font_small)
        self.tool_tag.pack(side="left", padx=6)
        self.bubble_text.bind("<Escape>", lambda _e: self.hide_all())
        self.bubble_text.bind("<Configure>", lambda _e: self.fit_bubble())

    def flash_copy(self) -> None:
        self.copy_btn.configure(text="Copied ✓", fg=ACCENT)
        self.root.after(1200, lambda: self.copy_btn.configure(text="Copy", fg=MUTED))

    def set_actions(self, actions: list[tuple[str, Callable[[], None]]],
                    stack: bool = False) -> None:
        """Pill buttons under the bubble text (e.g. Open tasks / Snooze).

        `stack` puts each button on its own full-width row (choice lists).
        """
        for child in self.action_row.winfo_children():
            child.destroy()
        for i, (label, fn) in enumerate(actions):
            primary = i == 0
            bg = ACCENT if primary else INPUT_BG
            btn = tk.Label(self.action_row, text=label, cursor="hand2", bg=bg,
                           fg=PANEL_BG if primary else PANEL_FG,
                           font=self.font_small, padx=10, pady=3,
                           anchor="w" if stack else "center")
            if stack:
                btn.pack(side="top", fill="x", pady=(0, 4))
            else:
                btn.pack(side="left", padx=(0, 6), pady=(0, 6))
            hover = ACCENT_HOVER if primary else HOVER_BG
            btn.bind("<Enter>", lambda _e, b=btn, c=hover: b.configure(bg=c))
            btn.bind("<Leave>", lambda _e, b=btn, c=bg: b.configure(bg=c))
            btn.bind("<Button-1>", lambda _e, f=fn: (self.fx.hide(self.bubble), f()))

    def set_bubble(self, text: str, tag: str = "", markdown: bool = False,
                   reveal: bool = False,
                   actions: list[tuple[str, Callable[[], None]]] | None = None,
                   stack: bool = False) -> None:
        """Replace bubble content; markdown replies can type themselves out."""
        self.set_actions(actions or [], stack)
        if self.reminder_hide_job:
            self.root.after_cancel(self.reminder_hide_job)
            self.reminder_hide_job = None
        t = self.bubble_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if markdown:
            plain = render_markdown(t, text)
        else:
            t.insert("end", text, tag)
            plain = text
        # shrink-to-fit width for short answers
        longest = max((len(line) for line in plain.split("\n")), default=0)
        t.configure(width=min(max(longest + 2, BUBBLE_MIN_CHARS), BUBBLE_CHARS))
        total = len(t.get("1.0", "end-1c"))
        if reveal and total > 12:
            t.tag_add("hidden", "1.0 + 1 chars", "end")
            self.reveal_total, self.reveal_shown = total, 1
            # long answers type faster; never longer than ~0.9 s
            self.reveal_start = time.perf_counter()
            self.reveal_dur = min(0.9, 0.25 + total / 700)
        else:
            self.reveal_total = self.reveal_shown = 0
        t.configure(state="disabled")
        self.fit_bubble()
        t.yview_moveto(0)

    def fit_bubble(self) -> None:
        """Size the bubble to its wrapped text (re-run once it has real width)."""
        t = self.bubble_text
        t.update_idletasks()
        breaks = t.count("1.0", "end-1c", "displaylines")
        height = min((breaks[0] if breaks else 0) + 1, BUBBLE_MAX_LINES)
        if int(t.cget("height")) != height:
            t.configure(height=height)
            if self.fx.shown(self.bubble):
                self.place_popups()

    def show_bubble(self) -> None:
        self.fx.hide(self.history)
        self.fx.hide(self.tasks_win)
        self.place_popups()
        self.fx.show(self.bubble, slide=1)

    def status(self, text: str, tag: str = "info") -> None:
        self.tool_tag.configure(text="")
        self.set_bubble(text, tag)
        self.show_bubble()

    def show_thinking(self, prompt: str) -> None:
        self.thinking_prompt = prompt
        self.stream_view = False
        self.stream_pending = None
        self.stream_shown = ""
        self.stream_width = 0
        short = prompt if len(prompt) <= 90 else prompt[:87] + "…"
        t = self.bubble_text
        t.configure(state="normal", width=BUBBLE_CHARS)
        t.delete("1.0", "end")
        t.insert("end", f"“{short}”\n", "prompt")
        t.mark_set("think", "end-1c")
        t.mark_gravity("think", "left")
        t.insert("end", "Thinking", "think")
        t.configure(state="disabled")
        self.reveal_total = self.reveal_shown = 0
        self.tool_tag.configure(text="")
        self.thinking_text = ""
        self.fit_bubble()
        self.show_bubble()

    def update_thinking(self, now: float) -> None:
        if self.stream_view:
            return
        elapsed = int(time.time() - self.busy_since)
        dots = "●" * (1 + int((now - self.t0) * 3) % 3)
        text = f"{self.busy_label}  {dots:<3}  {elapsed}s"
        if text == getattr(self, "thinking_text", ""):
            return
        self.thinking_text = text
        t = self.bubble_text
        try:
            t.configure(state="normal")
            t.delete("think", "end-1c")
            t.insert("think", text, "think")
            t.configure(state="disabled")
        except tk.TclError:  # bubble was replaced meanwhile
            pass

    def on_stream(self, text: str) -> None:
        """Worker thread sent the reply text so far; drawn on the next frame."""
        if self.busy:
            self.stream_pending = text

    def on_tool(self, caption: str) -> None:
        """A tool started: back to the magnifying glass with a caption."""
        if not self.busy:
            return
        self.busy_label = caption
        if self.stream_view:
            self.show_thinking(self.thinking_prompt)
        self.thinking_text = ""

    def draw_stream(self, now: float) -> None:
        """Render the streamed markdown so far, with a blinking block caret."""
        text = self.stream_pending
        if text is None or now - self.stream_drawn_at < STREAM_FRAME_S:
            return
        self.stream_pending = None
        self.stream_drawn_at = now
        if not text.strip() or text == self.stream_shown:
            return
        self.stream_view = True
        self.stream_shown = text
        t = self.bubble_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        plain = render_markdown(t, text)
        t.insert("end", " ▍", "stream_caret")
        # width only grows while streaming, so lines don't jump around
        longest = max((len(line) for line in plain.split("\n")), default=0)
        self.stream_width = max(self.stream_width,
                                min(max(longest + 3, BUBBLE_MIN_CHARS), BUBBLE_CHARS))
        t.configure(width=self.stream_width, state="disabled")
        self.fit_bubble()
        t.see("end")

    # ---------- full history ----------
    def build_history(self) -> None:
        self.history, inner = self.popup()
        header = tk.Frame(inner, bg=PANEL_BG)
        header.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(header, text="Chat history", bg=PANEL_BG, fg=ACCENT,
                 font=self.font_bold).pack(side="left")
        self.icon_button(header, "✕", lambda: self.fx.hide(self.history),
                         side="right")
        self.icon_button(header, "New chat", self.new_chat, side="right")
        self.icon_button(header, "Compact", self.compact_chat, side="right")
        self.ctx_label = tk.Label(inner, text="", bg=PANEL_BG, fg=MUTED,
                                  font=self.font_small, anchor="w")
        self.ctx_label.pack(fill="x", padx=14)
        self.log = tk.Text(inner, wrap="word", bg=PANEL_BG, fg=PANEL_FG,
                           relief="flat", font=self.font, padx=14, pady=6,
                           height=18, width=BUBBLE_CHARS + 4, cursor="arrow",
                           highlightthickness=0, selectbackground=ACCENT_DARK)
        self.log.pack(fill="both", expand=True, pady=(0, 8))
        setup_markdown(self.log, PANEL_FG, MUTED, ACCENT, INPUT_BG, self.font)
        self.log.tag_configure("who_you", foreground="#9CC2FF",
                               font=self.font_bold, spacing1=10)
        self.log.tag_configure("who_claude", foreground=ACCENT,
                               font=self.font_bold, spacing1=10)
        self.log.tag_configure("info", foreground=MUTED, spacing1=6)
        self.log.tag_configure("error", foreground="#FF8A80")
        self.log.configure(state="disabled")
        self.log.bind("<Escape>", lambda _e: self.hide_all())
        for line in self.chat.display:  # the chat from before a restart
            self.append(line["tag"], line["text"], line.get("who"), keep=False)
        self.update_ctx_label()

    def toggle_history(self) -> None:
        if self.fx.shown(self.history):
            self.fx.hide(self.history)
            return
        self.fx.hide(self.bubble)
        self.fx.hide(self.tasks_win)
        self.place_popups()
        self.fx.show(self.history, slide=1)
        self.log.see("end")

    def append(self, tag: str, text: str, who: str | None = None,
               keep: bool = True) -> None:
        if keep:
            self.chat.show(tag, text, who)
        self.log.configure(state="normal")
        if who:
            self.log.insert("end", who + "\n", f"who_{tag}")
        if tag == "claude":
            render_markdown(self.log, text.rstrip("\n"))
            self.log.insert("end", "\n")
        else:
            self.log.insert("end", text, tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    def new_chat(self) -> None:
        self.session_id = None
        self.state.pop("session_id", None)
        save_state(self.state)
        self.state.pop("pc_session", None)
        self.chat.clear()
        self.update_ctx_label()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.status("Fresh chat started.")

    # ---------- tokens and compaction ----------
    def update_ctx_label(self) -> None:
        """Token line under the history header: ↑ sent, ↓ received, context."""
        u = self.chat.usage
        if not (u["in"] or u["out"] or u["context"]):
            self.ctx_label.configure(text="No tokens used in this chat yet.", fg=MUTED)
            return
        text = f"↑ {chatlog.short(u['in'])} in   ↓ {chatlog.short(u['out'])} out"
        hot = u["context"] >= chatlog.AUTO_COMPACT_TOKENS
        if u["context"]:
            text += f"   ·   context {chatlog.short(u['context'])}"
            if u["window"]:
                pct = 100 * u["context"] / u["window"]
                text += f" / {chatlog.short(u['window'])} ({pct:.0f}%)"
                hot = hot or pct >= 70
        if self.chat.compactions:
            text += f"   ·   compacted {self.chat.compactions}×"
        self.ctx_label.configure(text=text, fg="#FFB74D" if hot else MUTED)

    def carry(self, text: str) -> str:
        """Prefix a fresh session's first message with the chat so far."""
        return self.chat.carry_over() + text

    def compact_chat(self) -> None:
        """Menu / history button: summarize the chat into a short note."""
        if self.busy or self.compacting:
            self.status("Busy right now. Compact once the answer is in.")
            return
        if not self.chat.can_compact():
            self.status("Nothing to compact yet. Chat a bit first.")
            return
        self.compacting = True
        self.status("🗜 Compacting the chat…")
        threading.Thread(target=self.compact_now, daemon=True).start()

    def compact_now(self, auto: bool = False) -> bool:
        """Summarize the chat (any thread). The next request starts a fresh
        session that gets the note instead of the whole history."""
        self.compacting = True
        try:
            text, covered = self.chat.compact_input()
            before = self.chat.usage["context"]
            summary, used, error = self.summarize(text)
            if not summary:
                self.post(lambda: self.status(f"Compaction failed: {error}", "error"))
                return False
            self.chat.apply_summary(summary, covered)
            self.chat.add_usage({"in": used.get("in"), "out": used.get("out")})
            self.session_id = None
            self.state.pop("session_id", None)
            self.state.pop("pc_session", None)
            after = self.chat.usage["context"]
            note = (f"🗜 {'Auto-compacted' if auto else 'Compacted'}: context "
                    f"{chatlog.short(before)} → {chatlog.short(after)} tokens "
                    f"(cost ↑ {chatlog.short(used.get('in') or 0)} "
                    f"↓ {chatlog.short(used.get('out') or 0)}).\n")

            def show() -> None:
                save_state(self.state)
                self.append("info", note)
                self.update_ctx_label()
                if not auto:
                    self.status(note.strip())
            self.post(show)
            return True
        finally:
            self.compacting = False

    def summarize(self, text: str) -> tuple[str | None, dict, str]:
        """(note, tokens used, error) from the cheapest model, no tools."""
        return self.cheap_call(chatlog.SUMMARY_PROMPT, text)

    def cheap_call(self, system: str, text: str) -> tuple[str | None, dict, str]:
        """One stateless request to the cheapest model, no tools (any thread)."""
        if self.provider != "claude":
            used: dict = {}
            reply = providers.one_shot(
                self.provider, system, text, "low",
                self.groq_model_var.get(), self.state.get("gemini_model", ""),
                usage_out=used)
            return reply, used, "no reply"
        cmd = [self.claude, "-p", "--model", MODEL, "--effort", "low",
               "--output-format", "json", "--tools", "", "--strict-mcp-config",
               "--setting-sources", "", "--disable-slash-commands",
               "--no-session-persistence", "--system-prompt", system]
        try:
            out = subprocess.run(cmd, input=text, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=180,
                                 cwd=WORK_DIR, creationflags=CREATE_NO_WINDOW,
                                 env={**os.environ, "CLAUDE_PET_CHILD": "1"})
            data = json.loads(out.stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return None, {}, str(exc)
        if data.get("is_error") or not data.get("result"):
            return None, {}, str(data.get("result") or out.stderr.strip()[-300:]
                                 or "no reply")
        return data["result"], claude_usage(data), ""

    # ---------- tasks panel ----------
    def build_tasks(self) -> None:
        self.tasks_win, inner = self.popup()
        header = tk.Frame(inner, bg=PANEL_BG)
        header.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(header, text="Tasks", bg=PANEL_BG, fg=ACCENT,
                 font=self.font_bold).pack(side="left")
        self.tasks_count = tk.Label(header, text="", bg=PANEL_BG, fg=MUTED,
                                    font=self.font_small)
        self.tasks_count.pack(side="left", padx=8)
        self.icon_button(header, "✕", lambda: self.fx.hide(self.tasks_win),
                         side="right")
        self.icon_button(header, "Clear done", self.clear_done, side="right")

        self.focus_strip = tk.Frame(inner, bg=INPUT_BG)
        self.focus_label = tk.Label(self.focus_strip, text="", bg=INPUT_BG,
                                    fg=PANEL_FG, font=self.font_small,
                                    anchor="w", justify="left",
                                    wraplength=TASKS_WIDTH - 70)
        self.focus_label.pack(side="left", fill="x", expand=True, padx=8, pady=5)
        self.focus_stop = tk.Label(self.focus_strip, text="Stop", bg=INPUT_BG,
                                   fg=MUTED, font=self.font_small,
                                   cursor="hand2", padx=8)
        self.focus_stop.pack(side="right", padx=4)
        self.focus_stop.bind("<Enter>", lambda _e: self.focus_stop.configure(fg=RED_SOFT))
        self.focus_stop.bind("<Leave>", lambda _e: self.focus_stop.configure(fg=MUTED))
        self.focus_stop.bind("<Button-1>", lambda _e: self.stop_focus())
        # hint + focus length stepper:  Hover a task, press ▶ Start   − 25 min +
        self.focus_hint = tk.Frame(inner, bg=PANEL_BG)
        tk.Label(self.focus_hint, text="Hover a task, press ▶ Start to focus",
                 bg=PANEL_BG, fg=MUTED, font=self.font_small,
                 anchor="w").pack(side="left")
        self.icon_button(self.focus_hint, "+",
                         lambda: self.step_focus_minutes(1), side="right")
        self.focus_len = tk.Label(self.focus_hint, bg=PANEL_BG, fg=ACCENT,
                                  font=self.font_small, cursor="xterm")
        self.focus_len.pack(side="right")
        self.focus_len.bind("<Button-1>", lambda _e: self.edit_focus_minutes())
        self.focus_len_entry = tk.Entry(self.focus_hint, width=4, bg=INPUT_BG,
                                        fg=PANEL_FG, relief="flat", **CARET,
                                        font=self.font_small, justify="center",
                                        highlightthickness=0,
                                        selectbackground=ACCENT_DARK)
        self.focus_len_entry.bind("<Return>", lambda _e: self.commit_focus_minutes())
        self.focus_len_entry.bind("<FocusOut>", lambda _e: self.commit_focus_minutes())
        self.focus_len_entry.bind("<Escape>", lambda _e: self.commit_focus_minutes(False))
        self.editing_focus_len = False
        self.focus_len_minus = self.icon_button(
            self.focus_hint, "−", lambda: self.step_focus_minutes(-1), side="right")
        self.update_focus_len()

        add = tk.Frame(inner, bg=INPUT_BG)
        self.task_add_frame = add
        add.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(add, text="+", bg=INPUT_BG, fg=ACCENT,
                 font=(self.font[0], 12, "bold")).pack(side="left", padx=(8, 4))
        self.task_entry = tk.Entry(add, bg=INPUT_BG, fg=PANEL_FG, relief="flat",
                                   font=self.font, **CARET,
                                   highlightthickness=0,
                                   selectbackground=ACCENT_DARK)
        self.task_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.task_hint = tk.Label(add, text="Add a task…   (@5pm sets a time)",
                                  bg=INPUT_BG, fg=MUTED, font=self.font)
        self.task_hint.place(x=30 + CARET_GAP, rely=0.5, anchor="w")
        self.task_hint.bind("<Button-1>", lambda _e: self.task_entry.focus_force())
        self.task_entry.bind("<KeyRelease>", lambda _e: self.update_task_hint())
        self.task_entry.bind("<Return>", lambda _e: self.add_from_entry())
        self.task_entry.bind("<Escape>", lambda _e: self.fx.hide(self.tasks_win))

        holder = tk.Frame(inner, bg=PANEL_BG)
        holder.pack(fill="both", expand=True, padx=6, pady=(0, 10))
        self.task_canvas = tk.Canvas(holder, bg=PANEL_BG, highlightthickness=0,
                                     width=TASKS_WIDTH, height=40)
        self.task_canvas.pack(fill="both", expand=True)
        self.task_list = tk.Frame(self.task_canvas, bg=PANEL_BG)
        self.task_canvas.create_window(0, 0, window=self.task_list, anchor="nw",
                                       width=TASKS_WIDTH)
        self.task_canvas.bind("<MouseWheel>", self.scroll_tasks)
        self.render_tasks()

    def scroll_tasks(self, event: tk.Event) -> None:
        self.task_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def update_task_hint(self) -> None:
        if self.task_entry.get():
            self.task_hint.place_forget()
        else:
            self.task_hint.place(x=30 + CARET_GAP, rely=0.5, anchor="w")

    def render_tasks(self) -> None:
        for child in self.task_list.winfo_children():
            child.destroy()
        items = self.tasks.ordered()
        left = len(self.tasks.pending())
        self.tasks_count.configure(
            text=f"{left} left" if left else ("all done" if items else ""))
        if not items:
            tk.Label(self.task_list, text="Nothing to do. Add your first task above.",
                     bg=PANEL_BG, fg=MUTED, font=self.font, pady=14
                     ).pack(fill="x")
        self.render_focus_strip()
        now = time.strftime("%H:%M")
        for task in items:
            self.task_row(task, now)
        self.task_list.update_idletasks()
        height = min(self.task_list.winfo_reqheight(), TASKS_MAX_LIST_PX)
        self.task_canvas.configure(height=height,
                                   scrollregion=(0, 0, TASKS_WIDTH,
                                                 self.task_list.winfo_reqheight()))
        if self.fx.shown(self.tasks_win):
            self.place_popups()

    def task_row(self, task: dict, now: str) -> None:
        done = task["done"]
        row = tk.Frame(self.task_list, bg=PANEL_BG, cursor="hand2")
        row.pack(fill="x", padx=4, pady=1)
        check = tk.Label(row, text="✓" if done else "○", width=2,
                         bg=PANEL_BG, fg=ACCENT if done else MUTED,
                         font=(self.font[0], 12, "bold" if done else "normal"))
        check.pack(side="left", padx=(4, 2), pady=4)
        font = tkfont.Font(family=self.font[0], size=self.font[1],
                           overstrike=done)
        text = tk.Label(row, text=task["text"], bg=PANEL_BG,
                        fg=MUTED if done else PANEL_FG, font=font,
                        wraplength=TASKS_WIDTH - 120, justify="left", anchor="w")
        text.pack(side="left", fill="x", expand=True, pady=4)
        text.font_ref = font  # keep the font alive with the label
        delete = tk.Label(row, text="✕", bg=PANEL_BG, fg=PANEL_BG,
                          font=self.font_small, cursor="hand2", padx=6)
        delete.pack(side="right")
        widgets = [row, check, text, delete]
        start = None
        focused = bool(self.focus and self.focus.get("mode") == "focus"
                       and self.focus.get("task_id") == task["id"])
        if focused:
            check.configure(text="◎", fg=ACCENT)
        elif not done:
            start = tk.Label(row, text="▶ Start", bg=PANEL_BG, fg=PANEL_BG,
                             font=self.font_small, cursor="hand2", padx=6)
            start.pack(side="right")
            start.bind("<Button-1>", lambda _e, i=task["id"]: self.start_focus(i))
            widgets.append(start)
        if task.get("due") and not done:
            overdue = task["due"] <= now and task.get("due_day") == time.strftime("%Y-%m-%d")
            label = time.strftime("%I:%M %p", time.strptime(task["due"], "%H:%M")).lstrip("0")
            chip = tk.Label(row, text=label, bg=INPUT_BG,
                            fg=RED_SOFT if overdue else MUTED,
                            font=self.font_small, padx=6)
            chip.pack(side="right", padx=4)
            widgets.append(chip)

        def hover(on: bool) -> None:
            bg = HOVER_BG if on else PANEL_BG
            for w in (row, check, text, delete):
                w.configure(bg=bg)
            delete.configure(fg=MUTED if on else bg)
            if start:
                start.configure(bg=bg, fg=ACCENT if on else bg)

        for w in widgets:
            w.bind("<Enter>", lambda _e: hover(True))
            w.bind("<Leave>", lambda _e: hover(False))
            w.bind("<MouseWheel>", self.scroll_tasks)
        for w in (row, check, text):
            w.bind("<Button-1>", lambda _e, i=task["id"]: self.toggle_task(i))
        delete.bind("<Button-1>", lambda _e, i=task["id"]: self.delete_task(i))

    def toggle_tasks(self) -> None:
        if self.fx.shown(self.tasks_win):
            self.fx.hide(self.tasks_win)
            return
        self.fx.hide(self.bubble)
        self.fx.hide(self.history)
        self.fx.hide(self.hover)
        self.fx.hide(self.hacks_win)
        self.render_tasks()
        self.place_popups()
        self.fx.show(self.tasks_win, slide=1)
        self.focus_input(self.tasks_win, self.task_entry)

    def add_from_entry(self) -> None:
        text = self.task_entry.get()
        self.task_entry.delete(0, "end")
        self.update_task_hint()
        self.add_task(text, quiet=True)

    def add_task(self, text: str, quiet: bool = False) -> None:
        task = self.tasks.add(text)
        if not task:
            return
        self.render_tasks()
        self.start_hop(0.3, 3 * ZOOM)
        if not quiet:
            due = f" (due {task['due']})" if task.get("due") else ""
            self.set_bubble(f"Added to your tasks: **{task['text']}**{due}",
                            markdown=True,
                            actions=[("Open tasks", self.toggle_tasks)])
            self.tool_tag.configure(text="Tasks")
            self.show_bubble()

    def toggle_task(self, task_id: str) -> None:
        task = self.tasks.toggle(task_id)
        self.render_tasks()
        if task and task["done"]:
            self.happy_until = time.time() + 2
            self.start_hop(0.45, 7 * ZOOM)
            if not self.tasks.pending():
                self.guard.snooze_until = 0
                self.last_reminder = time.time()

    def delete_task(self, task_id: str) -> None:
        self.tasks.delete(task_id)
        self.render_tasks()

    def clear_done(self) -> None:
        self.tasks.clear_done()
        self.render_tasks()

    # ---------- hackathons panel (shared with the hack-clock mod) ----------
    def build_hacks(self) -> None:
        self.hacks_win, inner = self.popup()
        header = tk.Frame(inner, bg=PANEL_BG)
        header.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(header, text="Hackathons", bg=PANEL_BG, fg=ACCENT,
                 font=self.font_bold).pack(side="left")
        self.hacks_count = tk.Label(header, text="", bg=PANEL_BG, fg=MUTED,
                                    font=self.font_small)
        self.hacks_count.pack(side="left", padx=8)
        self.icon_button(header, "✕", lambda: self.fx.hide(self.hacks_win),
                         side="right")

        add = self.hack_add_frame = tk.Frame(inner, bg=INPUT_BG)
        add.pack(fill="x", padx=12, pady=(0, 4))
        tk.Label(add, text="+", bg=INPUT_BG, fg=ACCENT,
                 font=(self.font[0], 12, "bold")).pack(side="left", padx=(8, 4))
        self.hack_entry = tk.Entry(add, bg=INPUT_BG, fg=PANEL_FG, relief="flat",
                                   font=self.font, **CARET,
                                   highlightthickness=0,
                                   selectbackground=ACCENT_DARK)
        self.hack_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.hack_hint = tk.Label(add, text="Paste a link (+ which competition), Enter",
                                  bg=INPUT_BG, fg=MUTED, font=self.font)
        self.hack_hint.place(x=30 + CARET_GAP, rely=0.5, anchor="w")
        self.hack_hint.bind("<Button-1>", lambda _e: self.hack_entry.focus_force())
        self.hack_entry.bind("<KeyRelease>", lambda _e: self.update_hack_hint())
        self.hack_entry.bind("<Return>", lambda _e: self.add_hack_from_entry())
        self.hack_entry.bind("<Escape>", lambda _e: self.fx.hide(self.hacks_win))
        self.hack_status = tk.Label(inner, text="", bg=PANEL_BG, fg=MUTED,
                                    font=self.font_small, anchor="w", justify="left",
                                    wraplength=HACKS_WIDTH - 10)
        self.hack_status.pack(fill="x", padx=14, pady=(0, 4))
        # Shown when a site blocks automatic reading: read the page text the person copied.
        self.hack_paste_bar = tk.Frame(inner, bg=PANEL_BG)
        self.icon_button(self.hack_paste_bar, "📋  Use copied page", self.paste_hack,
                         font=("Segoe UI Emoji", 9), fg=ACCENT, side="left")
        self.icon_button(self.hack_paste_bar, "Open page again",
                         lambda: self.hack_paste_url and os.startfile(self.hack_paste_url),
                         side="left")
        self.icon_button(self.hack_paste_bar, "Cancel", self.cancel_paste_hack, side="right")

        holder = tk.Frame(inner, bg=PANEL_BG)
        holder.pack(fill="both", expand=True, padx=6, pady=(0, 10))
        self.hack_canvas = tk.Canvas(holder, bg=PANEL_BG, highlightthickness=0,
                                     width=HACKS_WIDTH, height=40)
        self.hack_canvas.pack(fill="both", expand=True)
        self.hack_list = tk.Frame(self.hack_canvas, bg=PANEL_BG)
        self.hack_canvas.create_window(0, 0, window=self.hack_list, anchor="nw",
                                       width=HACKS_WIDTH)
        self.hack_canvas.bind("<MouseWheel>", self.scroll_hacks)
        self.render_hacks()

    def scroll_hacks(self, event: tk.Event) -> None:
        self.hack_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def update_hack_hint(self) -> None:
        if self.hack_entry.get():
            self.hack_hint.place_forget()
        else:
            self.hack_hint.place(x=30 + CARET_GAP, rely=0.5, anchor="w")

    def set_hack_status(self, text: str, fg: str = MUTED) -> None:
        self.hack_status.configure(text=text, fg=fg)
        if self.fx.shown(self.hacks_win):
            self.place_popups()

    def render_hacks(self) -> None:
        for child in self.hack_list.winfo_children():
            child.destroy()
        self.hack_bars = []
        self.hack_clocks = []
        self.hack_mtime = hackathons.mtime()
        now = hackathons.utc_now()
        rows = hackathons.ranked(hackathons.load(), now)
        viewing = next((e for e, _k, _a in rows if e.get("id") == self.hack_view), None)
        if viewing is not None:
            self.render_hack_detail(viewing, now)
            return
        self.hack_view = None
        if not self.hack_add_frame.winfo_ismapped():
            self.hack_add_frame.pack(fill="x", padx=12, pady=(0, 4),
                                     before=self.hack_status)
        active = sum(1 for _e, kind, _at in rows if kind in ("register", "starts", "submit"))
        self.hacks_count.configure(
            text=f"{active} active" if active else (f"{len(rows)} saved" if rows else ""))
        if not rows:
            tk.Label(self.hack_list, text="No hackathons yet. Paste a link above.",
                     bg=PANEL_BG, fg=MUTED, font=self.font, pady=14).pack(fill="x")
        for event, kind, at in rows:
            self.hack_row(event, kind, at, now)
        self.hack_list.update_idletasks()
        height = min(self.hack_list.winfo_reqheight(), HACKS_MAX_LIST_PX)
        self.hack_canvas.configure(height=height,
                                   scrollregion=(0, 0, HACKS_WIDTH,
                                                 self.hack_list.winfo_reqheight()))
        if self.fx.shown(self.hacks_win):
            self.place_popups()

    def hack_row(self, event: dict, kind: str, at: float | None, now: float) -> None:
        urgent = hackathons.is_urgent(kind, at, now)
        done = kind == "submitted"
        quiet = kind in ("missing", "closed")
        name_fg = RED_SOFT if urgent else HACK_GREEN if done else MUTED if quiet else ACCENT_HOVER
        row = tk.Frame(self.hack_list, bg=PANEL_BG)
        row.pack(fill="x", padx=8, pady=(4, 8))

        top = tk.Frame(row, bg=PANEL_BG)
        top.pack(fill="x")
        mark = "●" if urgent else "✔" if done else "◆"
        tk.Label(top, text=mark, bg=PANEL_BG, fg=name_fg,
                 font=self.font_small).pack(side="left", padx=(0, 6))
        eid = event.get("id", "")
        name = tk.Label(top, text=event.get("name", "Hackathon"), bg=PANEL_BG, fg=name_fg,
                        font=self.font_bold, anchor="w", cursor="hand2")
        name.pack(side="left")
        name.bind("<Button-1>", lambda _e: self.open_hack(eid))
        name.bind("<Enter>", lambda _e: name.configure(font=self.font_bold + ("underline",)))
        name.bind("<Leave>", lambda _e: name.configure(font=self.font_bold))
        if hackmode.active_id(self.hack_data) == eid:
            tk.Label(top, text="⚡ ON", bg=PANEL_BG, fg=HACK_GREEN,
                     font=self.font_small).pack(side="left", padx=6)
        clock = tk.Label(top, text=hackathons.countdown(event, kind, at, now),
                         bg=PANEL_BG, fg=RED_SOFT if urgent else PANEL_FG if not quiet else MUTED,
                         font=(self.font[0], 9, "bold" if urgent else "normal"))
        clock.pack(side="right")
        self.hack_clocks.append((clock, event))

        fraction = hackathons.progress(event, kind, at, now)
        if fraction is not None:
            self.hack_bar(row, fraction, urgent, done)
            tk.Label(row, text=f"{int(fraction * 100)}% of this stretch", bg=PANEL_BG,
                     fg=MUTED, font=self.font_small, anchor="w").pack(fill="x")

        details = [d for d in (
            (event.get("details") or {}).get("mode"),
            (event.get("details") or {}).get("prize"),
            f"Team {(event.get('details') or {}).get('teamSize')}"
            if (event.get("details") or {}).get("teamSize") else None) if d]
        if details:
            tk.Label(row, text="  ·  ".join(details), bg=PANEL_BG, fg=MUTED,
                     font=self.font_small, anchor="w").pack(fill="x")

        actions = tk.Frame(row, bg=PANEL_BG)
        actions.pack(fill="x", pady=(3, 0))
        registered = bool(event.get("isRegistered"))
        submitted = bool(event.get("isSubmitted"))
        self.icon_button(actions, "Registered ✓" if registered else "Mark registered",
                         lambda: self.mark_hack(eid, isRegistered=not registered),
                         fg=HACK_GREEN if registered else MUTED, side="left")
        self.icon_button(actions, "Submitted ✓" if submitted else "Mark submitted",
                         lambda: self.mark_hack(eid, isSubmitted=not submitted),
                         fg=HACK_GREEN if submitted else MUTED, side="left")
        link = hackathons.link_of(event, kind)
        if link:
            self.icon_button(actions, "Open ↗", lambda: os.startfile(link),
                             fg=ACCENT, side="left")
        self.icon_button(actions, "Workspace ›", lambda: self.open_hack(eid),
                         fg=ACCENT_HOVER, side="left")
        if self.hack_removing == eid:
            self.icon_button(actions, "Really remove?", lambda: self.remove_hack(eid),
                             fg=RED_SOFT, hover_fg=RED, side="right")
        else:
            self.icon_button(actions, "Remove", lambda: self.ask_remove_hack(eid),
                             side="right")
        for w in (row, top, actions):
            w.bind("<MouseWheel>", self.scroll_hacks)

    def shimmer_hacks(self, now: float) -> None:
        """A soft light sweeps along each bar, rows out of step, as on the strip."""
        for n, (bar, items, base, glow) in enumerate(self.hack_bars):
            sweep = ((now - self.t0) * 14 + n * 9) % (HACK_BAR_SEGMENTS + 16) - 8
            for i, item in enumerate(items):
                light = max(0.0, 1 - abs(i - sweep) / 4)
                bar.itemconfigure(item, fill=lerp_color(base[i], glow, light * 0.7)
                                  if light else base[i])

    def tick_hack_clocks(self) -> None:
        now = hackathons.utc_now()
        for label, event in self.hack_clocks:
            kind, at = hackathons.phase(event, now)
            label.configure(text=hackathons.countdown(event, kind, at, now))

    def hack_poll(self) -> None:
        """Pick up changes from the mod or another pet, and keep countdowns live."""
        try:
            if self.fx.shown(self.hacks_win):
                typing = self.root.focus_get() in getattr(self, "hack_inputs", ())
                if hackathons.mtime() != self.hack_mtime and not typing:
                    self.render_hacks()
                else:
                    self.tick_hack_clocks()
            self.hack_mode_tick()
        except (tk.TclError, KeyError):
            pass
        finally:
            self.root.after(1000, self.hack_poll)

    def toggle_hacks(self) -> None:
        if self.fx.shown(self.hacks_win):
            self.fx.hide(self.hacks_win)
            return
        for win in (self.bubble, self.history, self.hover, self.tasks_win):
            self.fx.hide(win)
        self.hack_removing = None
        self.render_hacks()
        self.place_popups()
        self.fx.show(self.hacks_win, slide=1)
        if self.hack_view is None:
            self.focus_input(self.hacks_win, self.hack_entry)

    # ---------- hackathon workspace and hackathon mode ----------
    def hack_event(self, eid: str | None) -> dict | None:
        return next((e for e in hackathons.load() if e.get("id") == eid), None) if eid else None

    def hack_ws(self, event: dict) -> dict:
        return hackmode.workspace(self.hack_data, event)

    def save_hack_data(self) -> None:
        hackmode.save(self.hack_data)
        active = self.hack_event(hackmode.active_id(self.hack_data))
        ws = self.hack_data.get("events", {}).get(active["id"]) if active else None
        self.hack_coach_dirs = ([ws["project"]] if ws and ws.get("coach")
                                and ws.get("project") else [])

    def open_hack(self, eid: str) -> None:
        """Show one hackathon's workspace in the hackathons panel."""
        self.hack_view = eid
        self.hack_linking = False
        if not self.fx.shown(self.hacks_win):
            self.toggle_hacks()
        else:
            self.render_hacks()

    def close_hack(self) -> None:
        self.hack_view = None
        self.render_hacks()

    def hack_bar(self, parent: tk.Widget, fraction: float, urgent: bool, done: bool,
                 width: int = HACKS_WIDTH - 40) -> None:
        """The segmented gradient bar (shimmers via shimmer_hacks)."""
        bar = tk.Canvas(parent, width=width, height=8, bg=PANEL_BG, highlightthickness=0)
        bar.pack(fill="x", pady=(5, 3))
        bar.create_rectangle(0, 2, width, 6, fill=HACK_TRACK, outline="")
        lo, hi, glow = ((HACK_RED_FROM, HACK_RED_TO, HACK_RED_GLOW) if urgent else
                        (HACK_DONE_FROM, HACK_DONE_TO, HACK_DONE_GLOW) if done else
                        (HACK_FROM, HACK_TO, HACK_GLOW))
        filled = round(HACK_BAR_SEGMENTS * max(0.0, min(1.0, fraction)))
        seg = width / HACK_BAR_SEGMENTS
        items, base = [], []
        for i in range(filled):
            color = lerp_color(lo, hi, i / max(1, HACK_BAR_SEGMENTS - 1))
            items.append(bar.create_rectangle(i * seg, 1, (i + 1) * seg + 0.5, 7,
                                              fill=color, outline=""))
            base.append(color)
        if filled:
            self.hack_bars.append((bar, items, base, glow))

    def hack_section(self, parent: tk.Widget, title: str, extra: str = "") -> tk.Frame:
        head = tk.Frame(parent, bg=PANEL_BG)
        head.pack(fill="x", padx=8, pady=(10, 2))
        tk.Label(head, text=title, bg=PANEL_BG, fg=ACCENT, font=self.font_bold).pack(side="left")
        if extra:
            tk.Label(head, text=extra, bg=PANEL_BG, fg=MUTED,
                     font=self.font_small).pack(side="left", padx=8)
        body = tk.Frame(parent, bg=PANEL_BG)
        body.pack(fill="x", padx=8)
        return body

    def render_hack_detail(self, event: dict, now: float) -> None:
        """Everything about one hackathon: clock, links, project, checklist,
        notes, focus, AI helpers, hackathon mode and the prompt coach."""
        self.hack_add_frame.pack_forget()
        self.hack_inputs = []
        ws = self.hack_ws(event)
        eid = event["id"]
        kind, at = hackathons.phase(event, now)
        urgent = hackathons.is_urgent(kind, at, now)
        active = hackmode.active_id(self.hack_data) == eid
        details = event.get("details") or {}
        page = self.hack_list
        wrap = HACKS_WIDTH - 30

        nav = tk.Frame(page, bg=PANEL_BG)
        nav.pack(fill="x", padx=4, pady=(2, 0))
        self.icon_button(nav, "‹ All hackathons", self.close_hack, side="left")
        self.icon_button(nav, "⚡ Hackathon mode ON" if active else "⚡ Start hackathon mode",
                         lambda: self.set_hack_mode(None if active else eid),
                         fg=HACK_GREEN if active else ACCENT, side="right")

        tk.Label(page, text=event.get("name", "Hackathon"), bg=PANEL_BG,
                 fg=RED_SOFT if urgent else ACCENT_HOVER, anchor="w", justify="left",
                 font=(self.font[0], 13, "bold"), wraplength=wrap).pack(fill="x", padx=8,
                                                                       pady=(4, 0))
        clock = tk.Label(page, text=hackathons.countdown(event, kind, at, now), bg=PANEL_BG,
                         fg=RED_SOFT if urgent else PANEL_FG, anchor="w",
                         font=(self.font[0], 11, "bold"))
        clock.pack(fill="x", padx=8)
        self.hack_clocks.append((clock, event))
        fraction = hackathons.progress(event, kind, at, now)
        if fraction is not None:
            holder = tk.Frame(page, bg=PANEL_BG)
            holder.pack(fill="x", padx=8)
            self.hack_bar(holder, fraction, urgent, kind == "submitted")

        def when(key: str) -> str:
            t = hackathons._instant(event.get(key))
            return time.strftime("%a %d %b, %I:%M %p", time.localtime(t)) if t else "?"
        dates = [f"Register by {when('registrationDeadline')}" if not event.get("isRegistered")
                 else "Registered ✓",
                 f"Starts {when('start')}", f"Submit by {when('submissionDeadline')}"]
        tk.Label(page, text="\n".join(dates), bg=PANEL_BG, fg=MUTED, font=self.font_small,
                 anchor="w", justify="left").pack(fill="x", padx=8, pady=(2, 0))
        facts = [str(v) for v in (details.get("mode"), details.get("location"),
                                  details.get("prize"),
                                  f"Team {details['teamSize']}" if details.get("teamSize")
                                  else None) if v]
        if facts:
            tk.Label(page, text="  ·  ".join(facts), bg=PANEL_BG, fg=MUTED,
                     font=self.font_small, anchor="w", justify="left",
                     wraplength=wrap).pack(fill="x", padx=8)
        if details.get("theme"):
            tk.Label(page, text=f"Theme: {details['theme']}", bg=PANEL_BG, fg=PANEL_FG,
                     font=self.font_small, anchor="w", justify="left",
                     wraplength=wrap).pack(fill="x", padx=8, pady=(2, 0))

        links = tk.Frame(page, bg=PANEL_BG)
        links.pack(fill="x", padx=4, pady=(4, 0))
        lk = event.get("links") or {}
        for label, url in (("Page ↗", event.get("url")), ("Register ↗", lk.get("registration")),
                           ("Submit ↗", lk.get("submission")),
                           ("Community ↗", details.get("community"))):
            if url:
                self.icon_button(links, label, lambda u=url: os.startfile(u), fg=ACCENT,
                                 side="left")
        self.icon_button(links, "Submitted ✓" if event.get("isSubmitted") else "Mark submitted",
                         lambda: self.mark_hack(eid, isSubmitted=not event.get("isSubmitted")),
                         fg=HACK_GREEN if event.get("isSubmitted") else MUTED, side="right")

        # project folder
        body = self.hack_section(page, "📁 Project")
        if ws.get("project"):
            row = tk.Frame(body, bg=PANEL_BG)
            row.pack(fill="x")
            tk.Label(row, text=Path(ws["project"]).name, bg=PANEL_BG, fg=PANEL_FG,
                     font=self.font_small).pack(side="left", padx=(4, 0))
            self.icon_button(row, "Change", lambda: self.hack_link_mode(True), side="right")
            self.icon_button(row, "Open in editor", lambda: self.open_hack_project(ws),
                             fg=ACCENT, side="right")
        if not ws.get("project") or self.hack_linking:
            tk.Label(body, text="Link the folder you build this in. The coach watches "
                     "Claude Code prompts there.", bg=PANEL_BG, fg=MUTED,
                     font=self.font_small, anchor="w", justify="left",
                     wraplength=wrap).pack(fill="x", padx=4)
            picks = tk.Frame(body, bg=PANEL_BG)
            picks.pack(fill="x")
            try:
                guesses = hackmode.guess_project(event, projects.load_index())
            except (OSError, ValueError):
                guesses = []
            for proj in guesses[:4]:
                self.icon_button(picks, f"📁 {proj['name']}",
                                 lambda pth=proj["path"]: self.link_hack_project(eid, pth),
                                 fg=ACCENT_HOVER, side="top", anchor="w")
            self.icon_button(picks, "Browse…", lambda: self.browse_hack_project(eid),
                             fg=ACCENT, side="top", anchor="w")

        # checklist
        done_n, total = hackmode.readiness(ws)
        body = self.hack_section(page, "✅ Submission checklist", f"{done_n}/{total} ready")
        if total:
            self.hack_bar(body, done_n / total, False, done_n == total, width=HACKS_WIDTH - 50)
        for item in ws["checklist"]:
            row = tk.Frame(body, bg=PANEL_BG)
            row.pack(fill="x")
            box = tk.Label(row, text=("☑  " if item["done"] else "☐  ") + item["text"],
                           bg=PANEL_BG, fg=MUTED if item["done"] else PANEL_FG,
                           font=self.font_small + (("overstrike",) if item["done"] else ()),
                           anchor="w", justify="left", wraplength=wrap - 40, cursor="hand2")
            box.pack(side="left", fill="x", expand=True, padx=(4, 0), pady=1)
            box.bind("<Button-1>", lambda _e, i=item["id"]: self.hack_check(eid, i))
            self.icon_button(row, "✕", lambda i=item["id"]: self.hack_uncheck_remove(eid, i),
                             side="right")
        add = tk.Entry(body, bg=INPUT_BG, fg=PANEL_FG, relief="flat", font=self.font_small,
                       **CARET, highlightthickness=0, selectbackground=ACCENT_DARK)
        add.pack(fill="x", padx=4, pady=(4, 0), ipady=4)
        add.insert(0, "")
        add.bind("<Return>", lambda _e: self.hack_add_item(eid, add.get()))
        self.hack_inputs.append(add)
        tk.Label(body, text="Type an item and press Enter to add it", bg=PANEL_BG, fg=MUTED,
                 font=self.font_small, anchor="w").pack(fill="x", padx=4)

        # focus
        mins = ws.get("focus_minutes", 0)
        body = self.hack_section(page, "🎯 Focus",
                                 f"{ws.get('sprints', 0)} sprints · {mins // 60}h {mins % 60}m")
        nxt = (hackmode.open_items(ws) or ["build"])[0]
        self.icon_button(body, f"Start a {self.focus_minutes()} min sprint on: {nxt}",
                         lambda: self.start_hack_sprint(eid), fg=ACCENT, side="top",
                         anchor="w")

        # AI helpers
        body = self.hack_section(page, "🤖 Helpers")
        line = body
        for i, (label, key) in enumerate((("💡 Winning ideas", "ideas"), ("🧭 What next?", "next"),
                                          ("📝 README draft", "readme"),
                                          ("🎬 Demo script", "demo"),
                                          ("🎤 2-min pitch", "pitch"),
                                          ("⚖ Judge my project", "judge"))):
            if i % 3 == 0:  # three per line
                line = tk.Frame(body, bg=PANEL_BG)
                line.pack(fill="x")
            self.icon_button(line, label, lambda k=key: self.hack_helper(eid, k),
                             fg=ACCENT_HOVER, side="left", padx=1, pady=1)

        # prompt coach
        body = self.hack_section(page, "💬 Prompt coach",
                                 f"{ws.get('coached', 0)} prompts coached")
        on = bool(ws.get("coach"))
        self.icon_button(body, ("● On" if on else "○ Off")
                         + " · coach my Claude Code prompts in this project",
                         lambda: self.toggle_hack_coach(eid), fg=HACK_GREEN if on else MUTED,
                         side="top", anchor="w")
        why = ("Works while hackathon mode is on and a project is linked. Each prompt "
               "you send there goes to the cheapest model for a sharper version.")
        if not self.watch_var.get():
            why += " Turn on the Claude Code session watcher in the menu too."
        tk.Label(body, text=why, bg=PANEL_BG, fg=MUTED, font=self.font_small, anchor="w",
                 justify="left", wraplength=wrap).pack(fill="x", padx=4)

        # notes
        body = self.hack_section(page, "🗒 Notes")
        notes = tk.Text(body, height=4, wrap="word", bg=INPUT_BG, fg=PANEL_FG, relief="flat",
                        font=self.font_small, **CARET, highlightthickness=0, padx=6, pady=4,
                        selectbackground=ACCENT_DARK)
        notes.pack(fill="x", padx=4, pady=(0, 8))
        notes.insert("1.0", ws.get("notes", ""))
        notes.bind("<KeyRelease>", lambda _e: self.hack_notes_changed(eid, notes))
        self.hack_inputs.append(notes)

        def wheel(w: tk.Widget) -> None:
            if not isinstance(w, tk.Text):
                w.bind("<MouseWheel>", self.scroll_hacks)
            for c in w.winfo_children():
                wheel(c)
        wheel(page)
        self.hack_list.update_idletasks()
        height = min(self.hack_list.winfo_reqheight(), HACKS_MAX_LIST_PX + 120)
        self.hack_canvas.configure(height=height, scrollregion=(
            0, 0, HACKS_WIDTH, self.hack_list.winfo_reqheight()))
        self.hacks_count.configure(text="workspace")
        if self.fx.shown(self.hacks_win):
            self.place_popups()

    # --- workspace actions ---
    def hack_check(self, eid: str, item_id: str) -> None:
        event = self.hack_event(eid)
        if not event:
            return
        ws = self.hack_ws(event)
        hackmode.toggle_item(ws, item_id)
        self.save_hack_data()
        done_n, total = hackmode.readiness(ws)
        if done_n == total and total:
            self.happy_until = self.sparkle_until = time.time() + 3
            self.start_hop(0.5, 8 * ZOOM)
        else:
            self.start_hop(0.3, 3 * ZOOM)
        self.render_hacks()

    def hack_uncheck_remove(self, eid: str, item_id: str) -> None:
        event = self.hack_event(eid)
        if event:
            hackmode.remove_item(self.hack_ws(event), item_id)
            self.save_hack_data()
            self.render_hacks()

    def hack_add_item(self, eid: str, text: str) -> None:
        event = self.hack_event(eid)
        if event and text.strip():
            hackmode.add_item(self.hack_ws(event), text)
            self.save_hack_data()
            self.render_hacks()

    def hack_notes_changed(self, eid: str, widget: tk.Text) -> None:
        job = getattr(self, "hack_notes_job", None)
        if job:
            self.root.after_cancel(job)

        def store() -> None:
            event = self.hack_event(eid)
            if event:
                self.hack_ws(event)["notes"] = widget.get("1.0", "end-1c")
                self.save_hack_data()
        self.hack_notes_job = self.root.after(700, store)

    def hack_link_mode(self, on: bool) -> None:
        self.hack_linking = on
        self.render_hacks()

    def link_hack_project(self, eid: str, path: str) -> None:
        event = self.hack_event(eid)
        if not event:
            return
        self.hack_ws(event)["project"] = path
        self.hack_linking = False
        self.save_hack_data()
        self.render_hacks()

    def browse_hack_project(self, eid: str) -> None:
        path = filedialog.askdirectory(title="Folder you build this hackathon in",
                                       initialdir=str(projects.ROOTS[0])
                                       if projects.ROOTS else str(Path.home()))
        if path:
            self.link_hack_project(eid, str(Path(path)))

    def open_hack_project(self, ws: dict) -> None:
        path = Path(ws["project"])
        if not path.is_dir():
            self.set_hack_status("That folder is gone. Link it again.", RED_SOFT)
            return
        try:
            index = projects.load_index()
        except (OSError, ValueError):
            index = []
        project = next((p for p in index if Path(p["path"]) == path), None)
        if project is None:
            project = projects.describe(path, path.parent)
        self.open_project(project, "", path.name, direct=True)

    def toggle_hack_coach(self, eid: str) -> None:
        event = self.hack_event(eid)
        if event:
            ws = self.hack_ws(event)
            ws["coach"] = not ws.get("coach")
            self.save_hack_data()
            self.render_hacks()

    def set_hack_mode(self, eid: str | None) -> None:
        """Turn hackathon mode on for one event (or off with None)."""
        if eid:
            event = self.hack_event(eid)
            if not event:
                return
            self.hack_data["active"] = eid
            ws = self.hack_ws(event)
            self.hack_milestone_check = 0.0
            self.happy_until = self.sparkle_until = time.time() + 2.5
            self.start_hop(0.5, 8 * ZOOM)
            msg = (f"⚡ **Hackathon mode: {event.get('name')}**\n"
                   f"{hackmode.time_left_text(event, hackathons.utc_now())} to submit. "
                   "I'll keep the countdown under me, nudge you at the big deadlines, "
                   "and help in every chat with this hackathon in mind.")
            if not ws.get("project"):
                msg += "\nLink your project folder in the workspace to get the prompt coach."
            self.save_hack_data()
            self.set_hack_status(msg.replace("**", "").split("\n")[0], HACK_GREEN)
        else:
            self.hack_data.pop("active", None)
            self.save_hack_data()
            self.fx.hide(self.hack_badge)
            self.set_hack_status("Hackathon mode off.")
        self.render_hacks()

    def start_hack_sprint(self, eid: str) -> None:
        event = self.hack_event(eid)
        if not event:
            return
        ws = self.hack_ws(event)
        step = (hackmode.open_items(ws) or ["build"])[0]
        text = f"⚡ {event.get('name')}: {step}"
        task = next((t for t in self.tasks.items if t["text"] == text and not t["done"]), None)
        if task is None:
            task = self.tasks.add(text)
        if not task:
            return
        if hackmode.active_id(self.hack_data) != eid:
            self.set_hack_mode(eid)
        if self.focus:
            self.stop_focus()
        self.fx.hide(self.hacks_win)
        self.start_focus(task["id"])

    def hack_helper(self, eid: str, key: str) -> None:
        """Ask the pet's AI something about this hackathon, with its facts."""
        event = self.hack_event(eid)
        if not event or self.busy:
            return
        ws = self.hack_ws(event)
        details = event.get("details") or {}
        facts = (f"Hackathon: {event.get('name')} ({event.get('url')}). "
                 f"Theme: {details.get('theme') or 'open'}. Prize: {details.get('prize') or '?'}. "
                 f"Team: {details.get('teamSize') or '?'}. Mode: {details.get('mode') or '?'}. "
                 f"Time left to submit: {hackmode.time_left_text(event, hackathons.utc_now())}. "
                 f"Project folder: {ws.get('project') or 'not linked'}. "
                 f"Checklist still open: {'; '.join(hackmode.open_items(ws)) or 'nothing'}. "
                 f"My notes: {ws.get('notes') or 'none'}.")
        asks = {
            "ideas": "Give me 3 hackathon project ideas that could win this. For each: a "
                     "one-line pitch, why judges would like it, the smallest demo-able MVP, "
                     "and the tech to build it fast.",
            "next": "Given the time left and my checklist, what exactly should I do next? "
                    "Give an ordered plan for the remaining time, in time blocks, and what "
                    "to cut if I fall behind.",
            "readme": "Draft the README for my hackathon project: problem, solution, how it "
                      "works, tech stack, setup steps, demo link placeholder, team. Look at "
                      "the project folder if you can.",
            "demo": "Write a 2-minute demo video script for my project: what to show on "
                    "screen second by second, and the voice-over lines.",
            "pitch": "Write a punchy 2-minute pitch for the judges: hook, problem, solution, "
                     "demo moment, impact, ask.",
            "judge": "Act as a tough hackathon judge. Score my project on innovation, "
                     "technical depth, design, impact and presentation (1-10 each) from "
                     "what you can see, and give the 3 fixes that would raise the score most.",
        }
        shown = {"ideas": "💡 Winning ideas", "next": "🧭 What next?", "readme": "📝 README draft",
                 "demo": "🎬 Demo script", "pitch": "🎤 2-min pitch",
                 "judge": "⚖ Judge my project"}[key]
        self.fx.hide(self.hacks_win)
        self.show_bar()
        prompt = f"{facts}\n\n{asks[key]}"
        uses_files = key in ("readme", "judge") and ws.get("project")
        self.send(prompt, False, shown=f"{shown} · {event.get('name')}",
                  route=False, scoped="Read,Glob,Grep" if uses_files else False)

    # --- hackathon mode: badge, nudges, coach ---
    def build_hack_badge(self) -> None:
        self.hack_badge, inner = self.popup()
        self.hack_badge_label = tk.Label(inner, text="", bg=PANEL_BG, fg=HACK_GREEN,
                                         font=("Segoe UI Emoji", 9), padx=10, pady=3,
                                         cursor="hand2")
        self.hack_badge_label.pack()
        self.hack_badge_label.bind(
            "<Button-1>", lambda _e: self.open_hack(hackmode.active_id(self.hack_data)))
        self.hack_badge_label.bind("<Enter>", lambda _e: self.hover_enter())

    def hack_mode_tick(self) -> None:
        """Every second: badge text and place, deadline nudges."""
        eid = hackmode.active_id(self.hack_data)
        event = self.hack_event(eid) if eid else None
        if not event:
            if eid:  # the hackathon was removed
                self.set_hack_mode(None)
            return
        now = hackathons.utc_now()
        kind, at = hackathons.phase(event, now)
        urgent = hackathons.is_urgent(kind, at, now)
        self.hack_badge_label.configure(text=hackmode.badge_text(event, now),
                                        fg=RED_SOFT if urgent else HACK_GREEN)
        covered = any(self.fx.shown(w) for w in (self.bar, self.hover)) or self.drag_start
        if covered or self.focus:
            self.fx.hide(self.hack_badge)
        elif not self.fx.shown(self.hack_badge):
            self.place_popups()
            self.fx.show(self.hack_badge, slide=-1)
        if now - self.hack_milestone_check < 30:
            return
        self.hack_milestone_check = now
        ws = self.hack_ws(event)
        due = hackmode.due_milestone(event, ws, now)
        if due:
            self.save_hack_data()
            actions = [("Workspace", lambda: self.open_hack(eid))]
            link = (event.get("links") or {}).get("submission") or event.get("url")
            if link and due[0] <= 2:
                actions.insert(0, ("Open submit page", lambda: os.startfile(link)))
            done_n, total = hackmode.readiness(ws)
            self.remind(f"⏰ **{event.get('name')}**\n{due[1]}\n"
                        f"*Checklist: {done_n}/{total} ready.*", actions,
                        speak=due[1], shake=due[0] <= 2)

    def coach_prompt_event(self, prompt: str, project: str) -> None:
        """A prompt was typed into Claude Code in the linked project folder."""
        eid = hackmode.active_id(self.hack_data)
        event = self.hack_event(eid)
        if not event or not hackmode.worth_coaching(prompt):
            return
        ws = self.hack_ws(event)
        now = time.time()
        if not ws.get("coach") or now - self.hack_coach_last < hackmode.COACH_GAP_SECONDS:
            return
        self.hack_coach_last = now
        self.sparkle_until = now + 1.5
        text = hackmode.coach_prompt(event, ws, prompt, hackathons.utc_now())

        def work() -> None:
            reply, used, error = self.cheap_call(
                "You are a sharp, brief prompt coach for AI coding agents.", text)
            tip = hackmode.parse_coach(reply or "")
            self.post(lambda: self.show_coach(eid, tip, used, project))

        threading.Thread(target=work, daemon=True).start()

    def show_coach(self, eid: str, tip: dict | None, used: dict, project: str) -> None:
        if not tip:
            return
        event = self.hack_event(eid)
        if event:
            self.hack_ws(event)["coached"] = self.hack_ws(event).get("coached", 0) + 1
            self.save_hack_data()
        if self.busy:
            return  # don't cover an answer you're waiting for
        score = f" · {tip['score']}/10" if tip["score"] else ""
        md = (f"💡 **Prompt coach**{score} · _{project}_\n"
              f"**Sharper prompt:**\n```\n{tip['better']}\n```")
        if tip["tips"]:
            md += "\n" + "\n".join(f"- {t}" for t in tip["tips"])
        cost = used.get("in", 0) + used.get("out", 0)
        if cost:
            md += f"\n_coach cost {chatlog.short(cost)} tokens_"

        def copy() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(tip["better"])
            self.tool_tag.configure(text="Copied ✓ paste it in Claude Code")
        actions = [("Copy prompt", copy), ("Coach off", lambda: self.toggle_hack_coach(eid))]
        self.cc_show(md, actions, shake=False, auto_hide=45, tag="Hackathon coach")

    def add_hack_from_entry(self) -> None:
        typed = self.hack_entry.get().strip()
        if not typed or not self.linked():
            return
        parts = hackathons.split_entry(typed)
        if parts is None:
            self.set_hack_status("Paste the full link, starting with https://", RED_SOFT)
            return
        url, focus = parts
        if self.hack_adding:
            self.set_hack_status("Still reading the last link…")
            return
        self.hack_entry.delete(0, "end")
        self.update_hack_hint()
        self.hack_adding = True
        self.hack_paste_focus = focus
        self.set_hack_status(f"Finding “{focus}” on the page with Claude Haiku 5.5…" if focus
                             else "Reading the page with Claude Haiku 5.5…", ACCENT)
        self.start_hop(0.3, 3 * ZOOM)

        self.read_hack(url, pasted=False)

    def read_hack(self, url: str, pasted: bool) -> None:
        focus = getattr(self, "hack_paste_focus", "")

        def work() -> None:
            ok, message = hackathons.add_url(self.claude, url, pasted, focus)
            if not ok and self.provider != "claude":
                message += ("\nHackathon links are read by Claude Code's hack-clock "
                            "mod, so Claude Code must be signed in too.")
            self.post(lambda: self.hack_added(url, ok, message))

        threading.Thread(target=work, daemon=True).start()

    def paste_hack(self) -> None:
        if self.hack_adding or not self.hack_paste_url:
            return
        self.hack_adding = True
        self.set_hack_status("Reading the copied page with Claude Haiku 5.5…", ACCENT)
        self.read_hack(self.hack_paste_url, pasted=True)

    def cancel_paste_hack(self) -> None:
        self.hack_paste_url = None
        self.hack_paste_bar.pack_forget()
        self.set_hack_status("")

    def hack_added(self, url: str, ok: bool, message: str) -> None:
        self.hack_adding = False
        if not ok and hackathons.is_blocked(message):
            # Every automatic route hit a bot check: the person's own browser can open it.
            self.hack_paste_url = url
            os.startfile(url)
            self.set_hack_status("This site blocks automatic reading, so I opened it in "
                                 "your browser. There, press Ctrl+A then Ctrl+C, then "
                                 "click Use copied page.", ACCENT_HOVER)
            self.hack_paste_bar.pack(fill="x", padx=12, pady=(0, 6),
                                     after=self.hack_status)
            self.place_popups()
            self.start_hop(0.3, 3 * ZOOM)
            return
        if ok:
            self.hack_paste_url = None
            self.hack_paste_bar.pack_forget()
        self.set_hack_status(message, PANEL_FG if ok else RED_SOFT)
        self.render_hacks()
        if ok:
            self.happy_until = time.time() + 2
            self.start_hop(0.45, 7 * ZOOM)
        else:
            self.start_shake()

    def mark_hack(self, event_id: str, **change: object) -> None:
        try:
            hackathons.update(event_id, **change)
        except (OSError, ValueError) as exc:
            self.set_hack_status(f"Could not save: {exc}", RED_SOFT)
            return
        if change.get("isSubmitted") is True:
            self.happy_until = time.time() + 2
            self.start_hop(0.5, 8 * ZOOM)
        self.render_hacks()

    def ask_remove_hack(self, event_id: str) -> None:
        self.hack_removing = event_id
        self.render_hacks()

    def remove_hack(self, event_id: str) -> None:
        self.hack_removing = None
        try:
            hackathons.remove(event_id)
        except (OSError, ValueError) as exc:
            self.set_hack_status(f"Could not save: {exc}", RED_SOFT)
            return
        self.set_hack_status("Removed.")
        self.render_hacks()

    # ---------- reminders + focus guard ----------
    def remind(self, md: str, actions: list | None = None, speak: str = "",
               shake: bool = False, auto_hide: int | None = None) -> None:
        """Pet pops up with a task reminder, whether or not the box is open."""
        if self.fx.shown(self.tasks_win):
            self.fx.hide(self.tasks_win)
        self.last_active = time.time()  # wakes the pet up
        self.set_bubble(md, markdown=True, reveal=True, actions=actions)
        self.tool_tag.configure(text="Tasks")
        self.show_bubble()
        if shake:
            self.start_shake(0.6)
        else:
            self.start_hop(0.42, 6 * ZOOM)
        if speak:
            self.speak(speak)
        if auto_hide:
            self.reminder_hide_job = self.root.after(
                auto_hide * 1000, lambda: self.fx.hide(self.bubble))

    def task_actions(self) -> list:
        if self.focusing():  # no snoozing your way out of a focus session
            return [("Open tasks", self.toggle_tasks)]
        return [("Open tasks", self.toggle_tasks),
                ("Snooze 10 min", self.guard.snooze)]

    # ---------- memory ----------
    def free_idle_memory(self) -> None:
        """Every minute: unload models nobody used for a while, and give RAM
        back to Windows once when the pet falls asleep."""
        try:
            freed = False
            try:
                import listener

                freed = listener.unload_if_idle()
            except ImportError:
                pass
            if self.sleeping() and not self.busy:
                if self.searcher.model is not None:
                    self.searcher.model = self.searcher.matrix = None
                    self.searcher.key = ()
                    freed = True
                if freed or not self.memory_trimmed:
                    release_memory()
                    self.memory_trimmed = True
            else:
                self.memory_trimmed = False
                if freed or not self.startup_trimmed:  # startup leaves garbage too
                    release_memory()
                    self.startup_trimmed = True
        finally:
            self.root.after(MEMORY_POLL_MS, self.free_idle_memory)

    # ---------- keystroke counter ----------
    def on_keys_toggle(self) -> None:
        self.save_flag("key_counter", self.keys_var)
        if self.keys_var.get():
            if not self.key_counter.start():
                self.status("Couldn't start the keystroke counter.", "error")
        else:
            self.count_keys(save=True)
            self.key_counter.stop()

    def count_keys(self, save: bool = False) -> None:
        """Move new presses into today's count; save about once a minute."""
        record, milestone = keys.add(self.state, self.key_counter.take())
        now = time.time()
        if save or record or milestone or now - self.keys_saved > 60:
            self.keys_saved = now
            save_state(self.state)
        if milestone:
            total = keys.stats(self.state)["total"]
            self.cc_show(f"⌨ **{keys.short(total)} keystrokes** typed with me! 🎉",
                         [("Stats", self.show_keys)], shake=False, auto_hide=8,
                         tag="Keystrokes")
        elif record:
            self.cc_show(f"⌨ **New record!** {keys.today_count(self.state):,} keys "
                         "today, your best day yet. 🔥",
                         [("Stats", self.show_keys)], shake=False, auto_hide=8,
                         tag="Keystrokes")

    def show_keys(self) -> None:
        self.count_keys()
        md = keys.summary(self.state)
        if not self.keys_var.get():
            md += "\n\n*Counting is off (menu > Keystrokes).*"
        self.cc_show(md, [], shake=False, auto_hide=None, tag="Keystrokes")

    # ---------- updates ----------
    def update_poll(self) -> None:
        try:
            self.update_check()
        finally:
            self.root.after(updater.CHECK_EVERY_SECONDS * 1000, self.update_poll)

    def update_check(self, manual: bool = False) -> None:
        """Ask about a newer release once a day, after an AI is linked."""
        if not self.app_version:
            if manual:
                self.status("Running from source: update with git pull.", "info")
            return
        if self.update_checking:
            return
        if not manual and (not self.linked()
                           or self.state.get("update_asked_day") == updater.today()):
            return
        self.update_checking = True
        if manual:
            self.status("Checking for updates…", "info")
        current = self.app_version

        def work() -> None:
            try:
                latest = updater.latest_version()
            except (OSError, ValueError, KeyError) as e:
                self.post(lambda: self.update_done(manual and f"Couldn't check for updates: {e}"))
                return
            if not updater.newer(latest, current):
                self.post(lambda: self.update_done(
                    manual and f"Boba is up to date (v{current})."))
                return
            notes = updater.release_notes(latest)
            self.post(lambda: (self.update_done(""), self.offer_update(latest, notes)))

        threading.Thread(target=work, daemon=True).start()

    def update_done(self, message: str | bool) -> None:
        self.update_checking = False
        if message:
            self.status(str(message), "info")

    def offer_update(self, latest: str, notes: str) -> None:
        self.state["update_asked_day"] = updater.today()  # "Later" = ask tomorrow
        save_state(self.state)
        md = f"✨ **Boba v{latest} is ready!** You have v{self.app_version}."
        if notes:
            md += f"\n\n{notes}"
        self.cc_show(md, [("Update now", lambda: self.update_now(latest)),
                          ("Later", self.update_later),
                          ("What's new", lambda: webbrowser.open(updater.RELEASES_PAGE))],
                     shake=False, auto_hide=None, tag="Update")

    def update_later(self) -> None:
        self.fx.hide(self.bubble)
        self.status("OK, I'll ask again tomorrow.", "info")

    def update_now(self, latest: str) -> None:
        log = pet_paths.DATA_DIR / "update.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            updater.start_update(log)
        except OSError as e:
            self.status(f"Update failed to start: {e}", "error")
            return
        self.cc_show(f"⏳ Updating to **v{latest}**… I'll be back in a moment.", [],
                     shake=False, auto_hide=None, tag="Update")
        self.root.after(1500, self.quit)

    def announce_update(self) -> None:
        """First start after an update: say so, with the release notes."""
        if not self.app_version:
            return
        last = self.state.get("last_version")
        self.state["last_version"] = self.app_version
        save_state(self.state)
        if not last or not updater.newer(self.app_version, last):
            return
        ver = self.app_version

        def work() -> None:
            notes = updater.release_notes(ver)
            md = f"🎉 **Updated to v{ver}!**" + (f"\n\n{notes}" if notes else "")
            self.post(lambda: self.cc_show(md, [], shake=False, auto_hide=20, tag="Update"))

        threading.Thread(target=work, daemon=True).start()

    # ---------- water reminder ----------
    def on_water_toggle(self) -> None:
        self.save_flag("water", self.water_var)
        self.water_next = time.time() + water.INTERVAL_SECONDS
        if self.water_var.get():
            self.status("Water reminder on: one glass every hour.", "info")

    def check_water(self) -> None:
        if not self.water_var.get():
            return
        if water.due(self.water_next, time.time(), water.idle_seconds()):
            self.water_alarm()

    def water_alarm(self) -> None:
        """Ring, show the bubble, then say it out loud after the chime."""
        self.water_next = time.time() + water.SNOOZE_SECONDS  # until answered
        n = water.glasses_today(self.state)
        water.chime()
        self.remind(f"💧 **Water time!** Drink **1 glass of water** now.\n"
                    f"*Today: {n}/{water.DAILY_GOAL} glasses.*",
                    [("I drank it ✓", self.drank_water),
                     ("Snooze 10 min", self.snooze_water)], shake=True)
        self.tool_tag.configure(text="Water")
        self.root.after(int(water.chime_seconds() * 1000) + 200,
                        lambda: self.speak(water.SPOKEN))

    def drank_water(self) -> None:
        n = water.add_glass(self.state)
        save_state(self.state)
        self.water_next = time.time() + water.INTERVAL_SECONDS
        self.stop_speaking()
        cheer = " Goal reached! 🎉" if n == water.DAILY_GOAL else ""
        self.remind(f"💧 Nice! **{n}/{water.DAILY_GOAL}** glasses today.{cheer}\n"
                    "Next reminder in 1 hour.", auto_hide=6)
        self.tool_tag.configure(text="Water")
        self.happy_until = self.sparkle_until = time.time() + 2.5

    def snooze_water(self) -> None:
        self.water_next = time.time() + water.SNOOZE_SECONDS
        self.stop_speaking()
        self.fx.hide(self.bubble)
        self.status("OK, I'll remind you about water in 10 minutes.", "info")

    # ---------- attachments: dropped files / text, screenshots ----------
    def build_attach_chip(self) -> None:
        """Small "📎 name ✕" chip above the text in the ask box."""
        self.attach_chip = tk.Label(self.entry.master, bg=INPUT_BG, fg=PANEL_FG,
                                    font=self.font_small, padx=8, pady=2,
                                    cursor="hand2", anchor="w")
        self.attach_chip.bind("<Button-1>", lambda _e: self.set_attachment(None))
        self.attach_chip.bind("<Enter>", lambda _e: self.attach_chip.configure(
            fg="#FF8A80"))
        self.attach_chip.bind("<Leave>", lambda _e: self.attach_chip.configure(
            fg=PANEL_FG))

    def set_attachment(self, att: dict | None) -> None:
        self.attachment = att
        if att:
            self.attach_chip.configure(text=f"{att['label']}   ✕")
            self.attach_chip.pack(anchor="w", pady=(0, 6), before=self.entry)
            hint = {"image": "Ask about this screenshot…",
                    "files": "Ask about this file…",
                    "text": "Ask about this text…"}[att["kind"]]
        else:
            self.attach_chip.pack_forget()
            hint = f"Ask {self.provider_name()}…"
        self.placeholder.configure(text=hint)
        if self.fx.shown(self.bar):
            self.place_popups()

    def ask_attachment(self, question: str) -> None:
        """Send the attachment with a question (empty: a sensible default)."""
        att = self.attachment
        self.set_attachment(None)
        if not att:
            return
        if att["kind"] == "image":
            q = question or SHOT_ACTIONS["Explain"]
            prompt = (f"Image file: {att['path']}\n"
                      f"Read (view) that image, then: {q}")
            self.send(prompt, False, scoped="Read", shown=f"📷 {q}",
                      media={"images": [att["path"]]})
        elif att["kind"] == "files":
            q = question or DROP_ACTIONS["Explain"]
            listing = "\n".join(att["paths"])
            prompt = f"File(s):\n{listing}\nRead them, then: {q}"
            self.send(prompt, False, scoped="Read,Glob,Grep",
                      shown=f"📄 {att['label']}: {q}", media={"files": att["paths"]})
        else:
            q = question or DROP_ACTIONS["Explain"]
            prompt = f"{q}\n\n<text>\n{att['text']}\n</text>"
            self.send(prompt, False, scoped=False, shown=f"📝 {att['label']}: {q}")

    def offer_attachment(self, att: dict, title: str, actions: dict) -> None:
        """Bubble with one-click actions, plus "Ask…" for your own question."""
        self.set_attachment(att)
        buttons = [(name, lambda q=q: self.ask_attachment(q))
                   for name, q in actions.items()]
        buttons.append(("Ask…", self.show_bar))
        self.cc_show(title, buttons, shake=False, auto_hide=None, tag="Drop")

    # drag & drop (tkinterdnd2)
    def enable_drops(self) -> None:
        if TkinterDnD is None:
            return
        try:
            TkinterDnD._require(self.root)
            for widget in (self.canvas, self.entry):
                widget.drop_target_register(DND_FILES, DND_TEXT)
                widget.dnd_bind("<<DropEnter>>", self.on_drop_enter)
                widget.dnd_bind("<<DropLeave>>", self.on_drop_leave)
                widget.dnd_bind("<<Drop>>", self.on_drop)
        except (tk.TclError, RuntimeError):
            pass  # tkdnd failed to load: everything else still works

    def on_drop_enter(self, event: tk.Event) -> str:
        self.drop_hover = True
        self.last_active = time.time()
        return event.action

    def on_drop_leave(self, event: tk.Event) -> str:
        self.drop_hover = False
        return event.action

    def on_drop(self, event: tk.Event) -> str:
        self.drop_hover = False
        if self.busy:
            self.status("Busy answering, drop it again in a moment.", "info")
            return event.action
        data = event.data or ""
        try:
            parts = [p for p in self.root.tk.splitlist(data) if p]
        except tk.TclError:
            parts = []
        paths = [p for p in parts if Path(p).exists()]
        if paths and len(paths) == len(parts):
            self.drop_files(paths)
        elif data.strip():
            self.drop_text(data)
        return event.action

    def drop_files(self, paths: list[str]) -> None:
        paths = [str(Path(p)) for p in paths[:5]]
        first = Path(paths[0])
        if len(paths) == 1 and first.suffix.lower() in IMAGE_EXTS:
            att = {"kind": "image", "path": paths[0], "label": f"🖼 {first.name}"}
            self.offer_attachment(att, f"🖼 **{first.name}**: what should I do?",
                                  SHOT_ACTIONS)
            return
        name = first.name + (f" +{len(paths) - 1}" if len(paths) > 1 else "")
        att = {"kind": "files", "paths": paths, "label": f"📄 {name}"}
        self.offer_attachment(att, f"📄 **{name}**: what should I do?", DROP_ACTIONS)

    def drop_text(self, text: str) -> None:
        text = text.strip()[:DROP_TEXT_LIMIT]
        flat = " ".join(text.split())
        label = "“" + (flat[:28] + "…" if len(flat) > 28 else flat) + "”"
        att = {"kind": "text", "text": text, "label": f"📝 {label}"}
        words = len(text.split())
        self.offer_attachment(att, f"📝 **{words} words** of text: what should I do?",
                              DROP_ACTIONS)

    # screenshot ask (Ctrl+Alt+S)
    def snip(self) -> None:
        if self.snipping:
            return
        self.snipping = True
        for win in (self.hover, self.bubble, self.history, self.tasks_win):
            self.fx.hide(win)
        self.root.after(140, lambda: SnipOverlay(self.root, ACCENT, self.font,
                                                 self.snip_done))

    def snip_done(self, rect: tuple[int, int, int, int] | None) -> None:
        if rect is None:
            self.snipping = False
            return
        # let the overlay disappear from the screen before capturing
        self.root.after(120, lambda: self.snip_capture(rect))

    def snip_capture(self, rect: tuple[int, int, int, int]) -> None:
        self.snipping = False
        try:
            path = save_shot(SHOTS_DIR, *rect)
        except OSError as exc:
            self.status(f"Couldn't save the screenshot: {exc}", "error")
            return
        att = {"kind": "image", "path": str(path),
               "label": f"📷 Screenshot  {rect[2]}×{rect[3]}"}
        self.offer_attachment(att, "📷 **Got it.** Ask anything, or:", SHOT_ACTIONS)
        self.show_bar()

    # ---------- morning briefing and news updates ----------
    def brief_poll(self) -> None:
        """The morning briefing at the first activity of the day, then a news
        update every NEWS_GAP_S while you're at the PC, NEWS_PER_DAY in all."""
        try:
            today = time.strftime("%Y-%m-%d")
            if (self.brief_var.get() and time.localtime().tm_hour >= BRIEF_HOUR
                    and idle_seconds() < BRIEF_IDLE_MAX
                    and not self.busy and not self.briefing):
                if self.state.get("briefing_day") != today:
                    self.start_briefing()
                elif (self.state.get("news_runs", 1) < NEWS_PER_DAY
                        and time.time() - self.state.get("news_last", 0) >= NEWS_GAP_S):
                    self.start_briefing(update=True)
        finally:
            self.root.after(BRIEF_POLL_MS, self.brief_poll)

    def start_briefing(self, update: bool = False) -> None:
        """Tasks plus the top AI & tech news. An `update` is news only, from
        items published since the last briefing, and shows nothing if there
        are none."""
        if self.briefing or not self.linked():
            return
        self.briefing = True
        today = time.strftime("%Y-%m-%d")
        last = self.state.get("briefing_day")
        week_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        since = last if last and week_ago <= last < today else yesterday
        after = self.state.get("news_last", 0) if update else 0
        if update:
            since = today
            self.state["news_runs"] = self.state.get("news_runs", 0) + 1
        else:
            self.state["briefing_day"] = today
            self.state["news_runs"] = 1
        self.state["news_last"] = time.time()
        save_state(self.state)
        head = "" if update else self.briefing_head()
        count = NEWS_UPDATE_COUNT if update else 5
        if not update:
            self.cc_show(head + "\n\n_🛰 Fetching today's AI & tech news…_", [],
                         shake=False, auto_hide=None, tag="Briefing")

        def work() -> None:
            # Real feeds, read by the pet; the model only picks and explains
            # items by number, so links and dates can't be made up.
            # Stories shown in recent briefings are skipped, so a re-dated
            # old article can't come back as today's news.
            items = news.collect(seen=self.state.get("news_seen") or {})
            if update:
                items = [i for i in items if i["date"].timestamp() >= after]
                if not items:  # nothing new since the last one: stay quiet
                    self.post(lambda: setattr(self, "briefing", False))
                    return
            if not items:
                self.post(lambda: self.brief_done(
                    head, None, "no news feed could be reached"))
                return
            count_now = min(count, len(items))
            shown: list[dict] = []
            text = ""
            for attempt in range(2):  # one retry: a single failed call shouldn't
                if attempt:           # leave the day with the plain fallback
                    time.sleep(20)
                data, error = self.run_model(news.pick_prompt(items, since, count_now),
                                             "", None, live=False)
                text = news.render(data["result"], items, shown) if data else ""
                if text:
                    break
            if data and text:
                data["result"] = text
            else:  # the model failed: ranked headlines, still with real links
                note = "\n_(AI picks unavailable, so these are the top headlines.)_"
                data = {"result": news.fallback(items, since, count_now, shown) + note,
                        "session_id": None}
                error = None
            seen = news.remember(self.state.get("news_seen") or {}, shown, today)
            self.post(lambda: (self.state.update(news_seen=seen), save_state(self.state)))
            self.post(lambda: self.brief_done(head, data, error, update))

        threading.Thread(target=work, daemon=True).start()

    def briefing_head(self) -> str:
        hour = time.localtime().tm_hour
        hello = ("Good morning" if hour < 12 else
                 "Good afternoon" if hour < 17 else "Good evening")
        pending = self.tasks.ordered()
        pending = [t for t in pending if not t["done"]]
        if not pending:
            return f"☀️ **{hello}!** No open tasks. Fresh start."
        lines = [f"- {t['text']}" + (f"  _(due {t['due']})_" if t.get("due") else "")
                 for t in pending[:5]]
        more = f"\n- _…and {len(pending) - 5} more_" if len(pending) > 5 else ""
        word = "task" if len(pending) == 1 else "tasks"
        return f"☀️ **{hello}!** {len(pending)} open {word}:\n" + "\n".join(lines) + more

    def brief_done(self, head: str, data: dict | None, error: str | None,
                   update: bool = False) -> None:
        if self.busy:  # don't cover an answer you're waiting for
            self.root.after(2000, lambda: self.brief_done(head, data, error, update))
            return
        self.briefing = False
        if data:
            self.brief_session = data.get("session_id")
            news = data["result"].strip()
        else:
            news = f"_Couldn't fetch the news: {error}_"
        if update:
            md = f"**🛰 AI & tech news update**\n{news}"
            self.append("claude", md + "\n", who="News update 🛰")
            actions = [("Ask more", self.brief_followup)]
        else:
            md = f"{head}\n\n**🛰 AI & tech news**\n{news}"
            self.append("claude", md + "\n", who="Morning briefing ☀️")
            actions = [("Ask more", self.brief_followup), ("Open tasks", self.toggle_tasks)]
        if not data:
            actions = [("Retry", lambda: self.start_briefing())]
        self.cc_show(md, actions, shake=False, auto_hide=None, tag="Briefing")
        if self.speak_var.get():
            self.speak("News update." if update
                       else head.split("\n", 1)[0].replace("*", ""))

    def brief_followup(self) -> None:
        """Continue in the briefing's chat so "tell me more about #2" works."""
        if self.brief_session:
            self.session_id = self.brief_session
            self.state["session_id"] = self.session_id
            self.state["last_used"] = time.time()
            save_state(self.state)
        self.show_bar()

    # ---------- Claude Code alerts ----------
    def cc_start_offset(self) -> int:
        """Skip events from before the pet started; trim a big old file."""
        try:
            size = CC_EVENTS_FILE.stat().st_size
        except OSError:
            return 0
        if size > 256 * 1024:
            try:
                CC_EVENTS_FILE.write_text("", encoding="utf-8")
                return 0
            except OSError:
                pass
        return size

    def cc_poll(self) -> None:
        try:
            self.cc_read()
            self.check_waiting()
            if self.cc_queue and not self.busy:
                self.cc_alert(self.cc_queue.pop(0))
        finally:
            self.root.after(CC_POLL_MS, self.cc_poll)

    def cc_read(self) -> None:
        try:
            size = CC_EVENTS_FILE.stat().st_size
        except OSError:
            return
        if size < self.cc_offset:  # file was trimmed
            self.cc_offset = 0
        if size == self.cc_offset:
            return
        with open(CC_EVENTS_FILE, "rb") as f:
            f.seek(self.cc_offset)
            chunk = f.read()
        end = chunk.rfind(b"\n") + 1  # leave a half-written line for later
        self.cc_offset += end
        for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.cc_track(event)
            if self.cc_var.get() and self.cc_wanted(event):
                # one alert per session: a newer event replaces a queued one
                self.cc_queue = [e for e in self.cc_queue
                                 if e.get("session") != event.get("session")]
                self.cc_queue.append(event)

    def cc_wanted(self, event: dict) -> bool:
        session = event.get("session", "")
        if event.get("kind") == "Stop":
            took = event.get("took")
            if took is not None and took < CC_MIN_TURN_SECONDS:
                return False
            self.cc_last_stop[session] = event.get("time", time.time())
            return True
        if event.get("type") == "idle_prompt":
            # "waiting for your input" right after a finish alert is a repeat
            last = self.cc_last_stop.get(session, 0)
            return event.get("time", time.time()) - last > 180
        return True

    def cc_alert(self, event: dict) -> None:
        project = event.get("project") or "a project"
        account = event.get("account", "")  # "" is the plain ~/.claude account
        who = f"Claude ({account})" if account else "Claude"
        actions = [("Go to it", lambda e=event: self.cc_focus(e))]
        if event.get("kind") == "Stop":
            took = event.get("took")
            when = ""
            if took:
                m, sec = divmod(int(took), 60)
                when = f" · {m}m {sec:02d}s" if m else f" · {sec}s"
            snippet = event.get("snippet", "")
            md = f"✅ **{who} finished** in **{project}**{when}"
            if snippet:
                md += f"\n> {snippet}"
            self.cc_show(md, actions, shake=False, auto_hide=20)
        else:
            message = event.get("message") or "Claude needs your input"
            if event.get("type") == "permission_prompt" or "permission" in message.lower():
                md = f"🔔 **{who} needs permission** in **{project}**\n{message}"
            else:
                md = f"💬 **{message}** in **{project}**"
                if account:
                    md += f" ({account})"
            self.cc_show(md, actions, shake=True, auto_hide=60)

    def cc_show(self, md: str, actions: list, shake: bool, auto_hide: int | None,
                tag: str = "Claude Code", stack: bool = False) -> None:
        if self.fx.shown(self.tasks_win):
            self.fx.hide(self.tasks_win)
        self.last_active = time.time()  # wakes the pet up
        self.set_bubble(md, markdown=True, reveal=True, actions=actions, stack=stack)
        self.tool_tag.configure(text=tag)
        self.show_bubble()
        if shake:
            self.start_shake(0.6)
        else:
            self.happy_until = time.time() + 2.5
            self.sparkle_until = self.happy_until
            self.start_hop(0.42, 6 * ZOOM)
        if auto_hide:
            self.reminder_hide_job = self.root.after(
                auto_hide * 1000, lambda: self.fx.hide(self.bubble))

    def cc_focus(self, event: dict) -> None:
        """Bring the window this Claude Code session runs in to the front."""
        pids = [int(p) for p in event.get("pids", [])]
        hwnd = cc_find_window(pids)
        if not hwnd:
            self.status("Couldn't find that Claude Code window.", "info")
            return
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)

    def task_poll(self) -> None:
        try:
            self.count_keys()
            self.check_water()
            self.check_tasks()
        finally:
            self.root.after(TASK_POLL_MS, self.task_poll)

    def check_tasks(self) -> None:
        for task in self.tasks.due_now():
            self.remind(f"⏰ **Due now:** {task['text']}", self.task_actions(),
                        speak=f"Reminder: {task['text']} is due now.")
        pending = self.tasks.pending()
        if self.focusing():
            self.check_focus_guard()
            return
        if not pending:
            return
        nxt = pending[0]["text"]
        left = len(pending)
        tasks_word = "task" if left == 1 else "tasks"
        if not self.guard_var.get():
            self.guard.since = None  # guard off: no half-counted distraction
        else:
            action, name, hwnd = self.guard.check(True, self.strict_var.get())
            app = display_name(name)
            if action == 1:
                self.remind(f"👀 You've been on **{app}** for a minute.\n"
                            f"You still have **{left} {tasks_word}**. "
                            f"Next up: **{nxt}**", self.task_actions())
                return
            if action == 2:
                self.remind(f"⏰ Still on **{app}**? Time to get back to "
                            f"**{nxt}**.", self.task_actions(),
                            speak=f"Hey, you are still on {app}. "
                                  f"Let's get back to {nxt}.", shake=True)
                return
            if action == 3:
                minimize(hwnd)
                self.remind(f"🚫 I minimized **{app}**. Focus on **{nxt}**, "
                            f"you've got this.", self.task_actions(),
                            speak=f"Back to work. {nxt}.", shake=True)
                return
        minutes = self.remind_var.get()
        if (minutes and not self.busy and self.guard.since is None
                and time.time() - self.last_reminder > minutes * 60):
            self.last_reminder = time.time()
            rest = "\n".join(f"- {t['text']}" for t in pending[1:4])
            more = f"\n{rest}" if rest else ""
            self.remind(f"📝 **{left} {tasks_word} left.** Next up: **{nxt}**"
                        f"{more}", self.task_actions(), auto_hide=25)


    # ---------- Claude Code session watcher ----------
    def on_watch_toggle(self) -> None:
        self.save_flag("cc_watch", self.watch_var)
        self.watching = self.watch_var.get()
        if not self.watching:
            self.cc_running, self.cc_waiting = [], {}

    def watch_loop(self) -> None:
        """Worker thread: read new transcript lines, hand results to Tk."""
        watcher = cc_watch.Watcher()
        while True:
            try:
                if self.watching:
                    watcher.prompt_dirs = list(self.hack_coach_dirs)
                    events = [(e.kind, e.session.id, e.session.project,
                               e.session.account, e.detail, e.took)
                              for e in watcher.poll()]
                    running = [{"id": s.id, "project": s.project,
                                "account": s.account} for s in watcher.running()]
                    writes = {sid: s.last_write for sid, s in watcher.sessions.items()}
                    self.post(lambda e=events, r=running, w=writes:
                              self.watch_update(e, r, w))
            except Exception:  # a bad transcript line must not stop the watcher
                pass
            time.sleep(WATCH_POLL_SECONDS)

    def watch_update(self, events: list[tuple], running: list[dict],
                     writes: dict[str, float]) -> None:
        if not self.watching:
            return
        self.cc_running = running
        self.cc_writes = writes
        for kind, sid, project, acct, detail, took in events:
            who = f"Claude ({acct})" if acct else "Claude"
            if kind == "prompt":
                self.coach_prompt_event(detail, project)
            elif kind == "finish":
                self.cc_waiting.pop(sid, None)
                if took >= 3:
                    self.celebrate(sid)
            elif kind == "interrupted":
                self.cc_waiting.pop(sid, None)
            elif kind == "tests_passed":
                self.celebrate(sid, hop=False)
            elif kind == "tool_error":
                self.worried_until = max(self.worried_until, time.time() + 2.5)
            elif kind in ("tests_failed", "api_error"):
                self.worried_until = time.time() + 6
                self.start_shake(0.6)
                if self.busy or not self.cc_var.get():
                    continue
                title = ("Tests failed" if kind == "tests_failed"
                         else f"{who} stopped: {detail}")
                md = f"😟 **{title}** in **{project}**"
                if kind == "tests_failed" and detail:
                    md += f"\n> {detail}"
                actions = []
                if self.cc_pids.get(sid):
                    actions = [("Go to it", lambda s=sid: self.cc_focus(
                        {"pids": self.cc_pids.get(s, [])}))]
                self.cc_show(md, actions, shake=True, auto_hide=25)

    def celebrate(self, session: str, hop: bool = True) -> None:
        """Happy face with sparkles, once per finish (hook + watcher agree)."""
        now = time.time()
        if now - self.celebrated.get(session, 0) < 8:
            return
        self.celebrated[session] = now
        self.last_active = now
        self.happy_until = self.sparkle_until = now + 2.5
        self.worried_until = 0.0
        if hop:
            self.start_hop(0.42, 6 * ZOOM)

    def cc_track(self, event: dict) -> None:
        """Hook events: remember each session's window, start/stop waving."""
        sid = event.get("session", "")
        if event.get("pids"):
            self.cc_pids[sid] = event["pids"]
        if event.get("kind") == "Stop":
            self.cc_waiting.pop(sid, None)
            if self.watching:
                self.celebrate(sid)
            return
        message = (event.get("message") or "").lower()
        if (self.watching and event.get("kind") == "Notification"
                and (event.get("type") == "permission_prompt" or "permission" in message)):
            self.cc_waiting[sid] = {"event": event, "since": event.get("time", time.time()),
                                    "hwnd": None}
            self.next_wave_hop = 0.0
            self.last_active = time.time()

    def check_waiting(self) -> None:
        """Stop waving once you look at the session, answer it, or it moves on."""
        if not self.cc_waiting:
            return
        now = time.time()
        fg = ctypes.windll.user32.GetForegroundWindow()
        for sid, wait in list(self.cc_waiting.items()):
            if wait["hwnd"] is None:
                wait["hwnd"] = cc_find_window(
                    [int(p) for p in wait["event"].get("pids", [])]) or 0
            looked = wait["hwnd"] and fg == wait["hwnd"] and now - wait["since"] > 1.5
            moved_on = self.cc_writes.get(sid, 0) > wait["since"] + 3
            if looked or moved_on or now - wait["since"] > WAIT_MAX_SECONDS:
                self.cc_waiting.pop(sid, None)

    # ---------- usage meter ----------
    def usage_loop(self) -> None:
        """Worker thread: rebuild the usage summary every minute (or on demand)."""
        tokens = usage.ClaudeTokens()
        while True:
            try:
                snap = usage.snapshot(tokens)
                self.post(lambda s=snap: self.usage_update(s))
            except Exception:  # unreadable logs: try again next time
                pass
            self.usage_now.wait(USAGE_POLL_SECONDS)
            self.usage_now.clear()
            time.sleep(1.5)  # let the provider's own logs land first

    def usage_entry(self, provider: str | None = None) -> dict:
        return self.usage.get(provider or self.provider) or {}

    def usage_update(self, snap: dict) -> None:
        self.usage = snap
        if self.usage_ring.shown:
            self.show_usage_ring()
        self.warn_usage()

    def warn_usage(self) -> None:
        """One bubble per limit window when it passes 80% and 95%."""
        names = {"claude": "Claude Code", "codex": "Codex"}
        for provider in dict.fromkeys(("claude", "codex", self.provider)):
            entry = self.usage.get(provider) or {}
            wins = dict(entry.get("windows") or {})
            if not wins and entry.get("pct") is not None:
                wins = {"day": {"pct": entry["pct"], "resets_at": time.strftime("%Y-%m-%d")}}
            for key, win in wins.items():
                level = max((lv for lv in usage.WARN_AT if win["pct"] >= lv), default=0)
                tag = f"{provider}:{key}:{win.get('resets_at')}:{level}"
                if not level or tag in self.usage_warned:
                    continue
                self.usage_warned = (self.usage_warned + [tag])[-40:]
                self.state["usage_warned"] = self.usage_warned
                save_state(self.state)
                if self.busy:
                    continue
                label = usage.WINDOWS.get(key, "daily")
                when = usage.reset_text(win.get("resets_at"))
                name = names.get(provider) or providers.SHORT.get(provider, provider)
                md = f"⚠️ **{name}** {label} limit at **{win['pct']:.0f}%**"
                if when:
                    md += f", resets {when}"
                self.worried_until = time.time() + 4
                self.cc_show(md, [("Show usage", self.show_usage)], shake=True,
                             auto_hide=30, tag="Usage")
                return

    def show_usage_ring(self) -> None:
        """Ring around the pet: how full the current AI's tightest limit is."""
        if not self.usage_var.get() or self.ring.shown:
            self.usage_ring.hide()
            return
        progress, text = usage.ring_text(self.usage_entry())
        colors = next(c for below, c in USAGE_COLORS if progress * 100 < below)
        self.place_ring()
        self.usage_ring.set(progress, text, False, colors)
        self.usage_ring.show()
        self.usage_ring.win.attributes("-topmost", True)
        self.root.lift()

    def show_usage(self) -> None:
        """Usage panel: limits and today's use for every AI with data."""
        self.usage_now.set()
        names = {**providers.PROVIDERS, "claude": "Claude Code", "codex": "Codex"}
        order = [self.provider] + [p for p in names if p != self.provider]
        parts = [usage.describe(p, names[p], self.usage[p]) for p in order
                 if p in self.usage and (p == self.provider or self.usage[p].get("windows")
                                         or self.usage[p].get("tokens")
                                         or self.usage[p].get("requests"))]
        claude = self.usage.get("claude") or {}
        if not claude.get("windows"):
            parts.append("Claude Code limits show here once the status line "
                         "is connected (Usage meter → Connect…) and a session runs.")
        self.cc_show("\n\n".join(parts) or "No usage yet today.", [], shake=False,
                     auto_hide=None, tag="Usage")
        self.happy_until = 0.0

    def connect_status_line(self) -> None:
        done, skipped = usage.install_status_line()
        msg = f"Claude Code status line set for {done} account(s)."
        if skipped:
            msg += f" {skipped} already had their own status line, left as is."
        self.status(msg + " Limits appear after the next Claude Code reply.", "info")

    # ---------- wake word ----------
    def start_wake(self) -> None:
        if self.wake or not self.wake_var.get() or wake is None or LiveListener is None:
            return
        if wake.available():
            return
        self.wake = wake.WakeWord(
            on_wake=lambda phrase: self.post(lambda: self.on_wake(phrase)),
            on_error=lambda msg: self.post(lambda: self.wake_error(msg)))
        self.wake.start()

    def stop_wake(self) -> None:
        if self.wake:
            self.wake.stop()
            self.wake = None

    def on_wake_toggle(self) -> None:
        self.save_flag("wake_word", self.wake_var)
        if not self.wake_var.get():
            self.stop_wake()
            return
        problem = (wake.available() if wake else "Wake word needs numpy.") or (
            "" if LiveListener else "Voice needs the pet's .venv (faster-whisper).")
        if problem:
            self.status(problem, "error")
            return
        self.start_wake()
        self.status('Say "Hey Bob" and ask away.', "info")

    def wake_error(self, message: str) -> None:
        self.wake = None
        self.status(message, "error")

    def on_wake(self, _phrase: str) -> None:
        """ "Hey Bob": open the box and listen; a pause sends the question."""
        if (self.busy or self.listening or self.finishing or self.snipping
                or self.drag_start):
            return  # tick un-pauses the wake word once the pet is free
        self.last_active = time.time()
        self.wake_session = True
        self.start_hop(0.3, 4 * ZOOM)
        self.talk()
        if self.wake_job:
            self.root.after_cancel(self.wake_job)
        self.wake_job = self.root.after(WAKE_SILENCE_MS, self.wake_timeout)

    def wake_phrase(self, gen: int) -> None:
        """A finished phrase after the wake word: send it."""
        if gen != self.listen_gen or not self.listening or not self.wake_session:
            return
        if self.entry_text().strip():
            self.send_pending = True
            self.stop_listening()

    def wake_timeout(self) -> None:
        self.wake_job = None
        if self.wake_session and self.listening and not self.entry_text().strip():
            self.cancel_listening()
            self.fx.hide(self.bar)

    # ---------- focus timer ----------
    def build_focus(self) -> None:
        self.ring = FocusRing(self.root, TRANSPARENT, round(SPRITE_PX * 0.64),
                              max(2, round(2 * DPI_SCALE)),
                              (self.font[0], 8, "bold"), PANEL_BG, PANEL_FG)
        self.ring.bind_click(self.toggle_tasks)
        self.glow = EdgeGlow(self.root, band=round(74 * DPI_SCALE))
        self.place_ring()
        if self.focus and self.focus.get("end", 0) > time.time():
            self.ring.show()  # resume a session that was running at quit
            if self.focus.get("mode") == "focus":
                self.glow.show()
            self.root.after(50, self.root.lift)
        elif self.focus:
            self.focus = None
            self.save_focus()

    def place_ring(self) -> None:
        for ring in (self.ring, getattr(self, "usage_ring", None)):
            if ring:
                ring.place(self.root.winfo_x() + SWAY_ROOM + SPRITE_PX / 2,
                           self.root.winfo_y() + HOP_ROOM + SPRITE_PX / 2)

    def focusing(self) -> bool:
        return bool(self.focus and self.focus.get("mode") == "focus")

    def save_focus(self) -> None:
        if self.focus:
            self.state["focus"] = self.focus
        else:
            self.state.pop("focus", None)
        save_state(self.state)

    # focus length: menu presets, the tasks panel stepper, or type any number
    @staticmethod
    def clamp_focus(minutes: object, fallback: int = FOCUS_SECONDS // 60) -> int:
        try:
            value = int(str(minutes).strip())
        except ValueError:
            value = fallback
        return max(FOCUS_MIN_MINUTES, min(FOCUS_MAX_MINUTES, value))

    def focus_minutes(self) -> int:
        return self.focus_min_var.get()

    def focus_menu_label(self) -> str:
        return f"Focus timer ({self.focus_minutes()} min)"

    def set_focus_minutes(self, minutes: object) -> None:
        value = self.clamp_focus(minutes, self.focus_minutes())
        self.focus_min_var.set(value)
        self.state["focus_minutes"] = value
        save_state(self.state)
        self.update_focus_len()

    def step_focus_minutes(self, direction: int) -> None:
        """- / +: 1 minute steps up to 10, then 5 minute steps."""
        m = self.focus_minutes()
        if direction > 0:
            m = m + 1 if m < 10 else (m // 5 + 1) * 5
        else:
            m = m - 1 if m <= 10 else math.ceil(m / 5 - 1) * 5
        self.set_focus_minutes(m)

    def update_focus_len(self) -> None:
        self.focus_len.configure(text=f"{self.focus_minutes()} min")

    def edit_focus_minutes(self) -> None:
        """Swap the minutes label for a small entry to type any length."""
        if self.editing_focus_len:
            return
        self.editing_focus_len = True
        self.focus_len_entry.delete(0, "end")
        self.focus_len_entry.insert(0, str(self.focus_minutes()))
        self.focus_len_entry.pack(side="right", before=self.focus_len, ipady=1)
        self.focus_len.pack_forget()
        self.focus_len_entry.focus_force()
        self.focus_len_entry.select_range(0, "end")

    def commit_focus_minutes(self, save: bool = True) -> None:
        if not self.editing_focus_len:
            return
        self.editing_focus_len = False
        text = self.focus_len_entry.get().lower().replace("min", "").strip()
        if save and text:
            self.set_focus_minutes(text)
        self.focus_len.pack(side="right", before=self.focus_len_entry)
        self.focus_len_entry.pack_forget()

    def focus_note(self, text: str) -> None:
        self.set_bubble(text, "info")
        self.tool_tag.configure(text="Focus")
        self.show_bubble()

    def custom_focus_minutes(self) -> None:
        """Menu "Custom...": open the tasks panel with the length ready to type."""
        if self.focus:
            self.focus_note("Stop the running session to change its length.")
            return
        if not self.tasks.pending():
            self.focus_note("Add a task first, then set the focus length "
                            "in the tasks panel.")
            return
        if not self.fx.shown(self.tasks_win):
            self.toggle_tasks()
        self.root.after(120, self.edit_focus_minutes)

    def focus_menu(self) -> None:
        """Menu entry: stop a running session, else open tasks to pick one."""
        if self.focus:
            self.stop_focus()
        elif not self.fx.shown(self.tasks_win):
            self.toggle_tasks()

    def start_focus(self, task_id: str) -> None:
        task = next((t for t in self.tasks.items if t["id"] == task_id), None)
        if not task:
            return
        self.commit_focus_minutes()
        minutes = self.focus_minutes()
        now = time.time()
        self.focus = {"mode": "focus", "task_id": task["id"],
                      "text": task["text"], "start": now, "minutes": minutes,
                      "end": now + minutes * 60}
        self.save_focus()
        self.guard.since, self.guard.level, self.guard.snooze_until = None, 0, 0
        self.place_ring()
        self.ring.show()
        self.glow.show()
        self.root.lift()
        self.render_tasks()
        self.happy_until = time.time() + 1.5
        self.remind(f"🎯 **Focus: {task['text']}**\n"
                    f"{minutes} minute{'s' if minutes != 1 else ''}, starting now. "
                    "I'll keep distractions away.", auto_hide=6)

    def stop_focus(self) -> None:
        if not self.focus:
            return
        was_break = self.focus.get("mode") == "break"
        self.focus = None
        self.save_focus()
        self.ring.hide()
        self.glow.hide()
        self.render_tasks()
        if not was_break:
            self.set_bubble("Focus timer stopped.", "info")
            self.tool_tag.configure(text="Focus")
            self.show_bubble()
            self.reminder_hide_job = self.root.after(
                3000, lambda: self.fx.hide(self.bubble))

    def finish_focus(self) -> None:
        """Session done: celebrate, then a 5 minute break."""
        assert self.focus is not None
        task_id, text = self.focus["task_id"], self.focus["text"]
        minutes = self.focus.get("minutes") or round(
            (self.focus["end"] - self.focus["start"]) / 60)
        hack = self.hack_event(hackmode.active_id(self.hack_data))
        if hack and text.startswith("⚡ "):
            ws = self.hack_ws(hack)
            ws["focus_minutes"] = ws.get("focus_minutes", 0) + minutes
            ws["sprints"] = ws.get("sprints", 0) + 1
            self.save_hack_data()
        today = time.strftime("%Y-%m-%d")
        if self.state.get("focus_day") != today:
            self.state["focus_day"], self.state["focus_count"] = today, 0
        self.state["focus_count"] = self.state.get("focus_count", 0) + 1
        count = self.state["focus_count"]
        now = time.time()
        self.focus = {"mode": "break", "task_id": task_id, "text": text,
                      "start": now, "end": now + BREAK_SECONDS}
        self.save_focus()
        self.glow.celebrate()
        self.ring.burst()
        self.render_tasks()
        for i, delay in enumerate((0, 520, 1040)):
            self.root.after(delay, lambda h=(7 - i) * ZOOM: self.start_hop(0.45, h))
        rounds = f"{count} focus session{'s' if count != 1 else ''} today. "
        open_task = any(t["id"] == task_id and not t["done"]
                        for t in self.tasks.items)
        actions = [("Mark done", lambda: self.toggle_task(task_id))] if open_task else []
        actions.append(("Skip break", self.end_break))
        self.remind(f"🎉 **{minutes} minute{'s' if minutes != 1 else ''} done!** "
                    f"Great work on **{text}**.\n"
                    f"{rounds}Take a 5 minute break: stretch, drink some water.",
                    actions, speak="Nice work! Focus session done. "
                                   "Take a five minute break.")
        self.happy_until = time.time() + 4

    def end_break(self) -> None:
        if not self.focus:
            return
        task_id, text = self.focus["task_id"], self.focus["text"]
        self.focus = None
        self.save_focus()
        self.ring.hide()
        self.render_tasks()
        still_open = any(t["id"] == task_id and not t["done"]
                         for t in self.tasks.items)
        actions = [("Start focus", lambda: self.start_focus(task_id))] if still_open else []
        actions.append(("Open tasks", self.toggle_tasks))
        self.remind("☕ **Break's over.** Ready for another round"
                    + (f" on **{text}**?" if still_open else "?"),
                    actions, speak="Break is over. Ready for another round?")

    def update_focus(self, now: float) -> None:
        """Per frame: ring progress, glow breathing, confetti, session end."""
        self.glow.step(now)
        self.ring.step(now)
        if not self.focus:
            return
        left = self.focus["end"] - time.time()
        if left <= 0:
            if self.focus["mode"] == "focus":
                self.finish_focus()
            else:
                self.end_break()
            return
        total = self.focus["end"] - self.focus["start"]
        on_break = self.focus["mode"] == "break"
        secs = math.ceil(left)
        clock = f"{secs // 60}:{secs % 60:02d}"
        self.ring.set(1 - left / total, clock, on_break)
        if self.fx.shown(self.tasks_win):
            label = self.focus_text(clock)
            if self.focus_label.cget("text") != label:
                self.focus_label.configure(text=label)

    def focus_text(self, clock: str) -> str:
        assert self.focus is not None
        if self.focus["mode"] == "break":
            return f"☕  Break · {clock} left"
        return f"🎯  {self.focus['text']} · {clock} left"

    def render_focus_strip(self) -> None:
        self.focus_strip.pack_forget()
        self.focus_hint.pack_forget()
        if self.focus:
            left = max(0, math.ceil(self.focus["end"] - time.time()))
            self.focus_label.configure(
                text=self.focus_text(f"{left // 60}:{left % 60:02d}"))
            self.focus_strip.pack(fill="x", padx=12, pady=(0, 8),
                                  before=self.task_add_frame)
        elif self.tasks.pending():
            self.focus_hint.pack(fill="x", padx=14, pady=(0, 6),
                                 before=self.task_add_frame)

    def check_focus_guard(self) -> None:
        """Focus session: guard always on, strict, reacts within seconds."""
        assert self.focus is not None
        nxt = self.focus["text"]
        mins = max(1, math.ceil((self.focus["end"] - time.time()) / 60))
        action, name, hwnd = self.guard.check(True, True, focus=True)
        app = display_name(name)
        if action == 1:
            self.remind(f"🎯 Focus session! **{app}** can wait.\n"
                        f"Back to **{nxt}**, {mins} min to go.",
                        self.task_actions(), shake=True)
        elif action == 2:
            self.remind(f"⏰ Still on **{app}** during focus time. "
                        f"I'll minimize it soon. Back to **{nxt}**.",
                        self.task_actions(),
                        speak=f"You're in a focus session. Close {app} "
                              f"and get back to {nxt}.", shake=True)
        elif action == 3:
            minimize(hwnd)
            self.remind(f"🚫 Minimized **{app}**. Focus time: **{nxt}**, "
                        f"{mins} min left.", self.task_actions(),
                        speak=f"Back to {nxt}.", shake=True)

    # ---------- project launcher ----------
    def project_index(self, force: bool = False) -> list[dict]:
        with self.project_lock:
            if force or self.project_list is None:
                self.project_list = projects.load_index(force)
            return self.project_list

    def warm_projects(self) -> None:
        """Startup: load (or build) the project index. The embedding model
        loads on the first project search instead, to keep startup RAM low."""
        try:
            self.project_index()
        except Exception:  # warming is best effort; the request retries
            pass
        try:  # companion extension that runs Claude in VS Code's terminal
            projects.ensure_extension(projects.pick_editor(self.editor_var.get()))
        except Exception:
            pass

    def rescan_projects(self) -> None:
        def work() -> None:
            found = self.project_index(force=True)
            self.post(lambda: self.status(f"Found {len(found)} projects.", "info"))
        self.status("Rescanning projects…", "info")
        threading.Thread(target=work, daemon=True).start()

    def project_request(self, prompt: str, query: str, voice: bool,
                        choose: bool = False) -> None:
        """Find the project a request means.

        A plain project name opens at once (no model call). Anything else
        is shortlisted by similarity search, Haiku picks the best 3 of the
        shortlist, and you choose. `choose` skips the name shortcut.
        """
        if self.busy or not self.linked():
            return
        self.append("you", prompt + "\n", who="You 🎤" if voice else "You")
        self.busy = True
        self.busy_since = time.time()
        self.busy_label = "Finding the project"
        self.show_thinking(prompt)

        def work() -> None:
            recent = self.state.get("project_recent", {})
            try:
                found = self.project_index()
                if not choose:
                    winner = projects.clear_winner(
                        query, projects.rank(query, found, recent))
                    if winner:
                        self.post(lambda: self.project_ready(prompt, query, winner))
                        return
                shortlist = self.searcher.shortlist(query, found, recent)
                top = projects.pick_top3(self.claude, query, shortlist,
                                         ask_fn=self.one_shot_fn())
            except Exception as e:  # never leave the pet stuck "busy"
                self.post(lambda e=e: self.project_failed(str(e)))
                return
            self.post(lambda: self.project_choices(prompt, query, shortlist, top))

        threading.Thread(target=work, daemon=True).start()

    def project_done(self) -> None:
        self.busy = False
        self.last_active = time.time()
        self.stream_view = False
        self.stream_pending = None

    def project_failed(self, error: str) -> None:
        self.project_done()
        self.set_bubble(f"Couldn't search projects: {error}", "error")
        self.show_bubble()
        self.start_shake()

    def project_choices(self, prompt: str, query: str, shortlist: list[dict],
                        top: list[dict] | None) -> None:
        """Show Haiku's 3 picks (or the closest 3 if Haiku was unavailable)."""
        took = time.time() - self.busy_since
        self.project_done()
        if top:
            head, tag = f"📂 **Which project?** Best matches for “{query}”:", \
                "🔎 similarity + Haiku"
        elif top is None:
            top = shortlist[:3]
            head, tag = f"📂 **Which project?** Closest to “{query}”:", "🔎 similarity only"
        else:
            top = shortlist[:3]
            head, tag = (f"📂 Nothing clearly fits “{query}”. Closest ones:",
                         "🔎 similarity + Haiku")
        # always offer 3: fill Haiku's picks up from the similarity order
        top = (top + [p for p in shortlist if p not in top])[:3]
        lines = [head]
        for i, p in enumerate(top, 1):
            where = f" · _{p['folder']}_" if p["folder"] else ""
            lines.append(f"{i}. **{p['name']}**{where}\n   {p['summary'][:80]}")
        actions = [(f"{i}  {p['name'][:34]}", lambda p=p: self.open_project(p, prompt, query))
                   for i, p in enumerate(top, 1)]
        actions.append(("Not a project, just ask Claude",
                        lambda: self.send(prompt, voice=False, route=False)))
        self.append("claude", "\n".join(lines) + "\n", who="Projects")
        self.set_bubble("\n".join(lines), markdown=True, actions=actions, stack=True)
        self.tool_tag.configure(text=f"{tag} · {took:.1f}s")
        self.show_bubble()
        self.start_hop(0.3, 3 * ZOOM)

    def project_ready(self, prompt: str, query: str, project: dict) -> None:
        """Exact-name match: open it now, with a way out if it's the wrong one."""
        self.project_done()
        self.open_project(project, prompt, query, direct=True)

    def open_project(self, project: dict, prompt: str, query: str,
                     direct: bool = False) -> None:
        editor = projects.pick_editor(self.editor_var.get())
        try:
            opened = projects.launch(project, editor)
        except OSError as e:
            self.set_bubble(f"Couldn't open {project['name']}: {e}", "error")
            self.show_bubble()
            self.start_shake()
            return
        recent = self.state.setdefault("project_recent", {})
        recent[project["path"]] = time.time()
        for old in sorted(recent, key=recent.get)[:-30]:  # keep the last 30
            del recent[old]
        save_state(self.state)
        md = f"📂 Opening **{project['name']}**\n{' · '.join(opened)}"
        self.append("claude", md + "\n", who="Projects")
        actions = [("Open folder", lambda: os.startfile(project["path"]))]
        if direct:
            actions.append(("Not this one?", lambda: self.project_request(
                prompt, query, voice=False, choose=True)))
        self.set_bubble(md, markdown=True, actions=actions)
        self.tool_tag.configure(text="🔎 name match · no tokens" if direct else "")
        self.show_bubble()
        self.happy_until = time.time() + 2.5
        self.start_hop(0.42, 6 * ZOOM)
        self.reminder_hide_job = self.root.after(15_000, lambda: self.fx.hide(self.bubble))

    # ---------- Claude Code call ----------
    def send(self, prompt: str, voice: bool, scoped: bool | str | None = None,
             shown: str | None = None, route: bool = True,
             media: dict | None = None) -> None:
        """Ask Claude. `scoped` pins the tool set (str list, or False for
        none) instead of the lite/tools routing; `shown` is the text shown
        in the bubble and history when the real prompt is long. Requests to
        open a project go to the project launcher unless `route` is False."""
        if not self.linked():
            return
        if route and scoped is None and (self.computer_mode == "off"
                                         or projects.EXPLICIT_PROJECT.search(prompt)):
            query = projects.launch_query(prompt)
            if query:
                self.project_request(prompt, query, voice)
                return
        if self.compacting:
            self.status("Compacting the chat, one moment…")
            return
        now = time.time()
        if providers.session_provider(self.session_id) != self.provider:
            self.session_id = None  # a chat from another provider can't resume here
        if self.session_id and now - self.state.get("last_used", now) > IDLE_RESET_SECONDS:
            self.session_id = None
            self.append("info", "Idle 30+ min: fresh session; it gets a short note of this chat.\n")
        self.state["last_used"] = now
        self.pending_prompt = prompt
        self.append("you", (shown or prompt) + "\n", who="You 🎤" if voice else "You")
        self.busy = True
        self.busy_since = now
        self.busy_label = "Looking at it" if scoped is not None else "Thinking"
        self.show_thinking(shown or prompt)
        self.work_gen += 1
        computer.CANCEL.clear()
        self.talk_back = voice or self.cont is not None or self.speak_var.get()
        self.spoke_plan = False
        threading.Thread(target=self.ask, args=(prompt, voice, scoped, media, self.work_gen),
                         daemon=True).start()

    def claude_cmd(self, tools: bool | str, session: str | None) -> list[str]:
        cmd = [self.claude, "-p", "--model", self.claude_model, "--effort", self.effort,
               "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--settings", PET_SETTINGS,
               "--disable-slash-commands", "--autocompact", AUTOCOMPACT_TOKENS]
        todo = self.tasks.summary()
        extra = f" The user's open to-do tasks: {todo}." if todo else ""
        extra += self.hack_context()
        if isinstance(tools, str):  # scoped: just these tools, pre-approved
            today = time.strftime("%Y-%m-%d")
            cmd += ["--tools", tools, "--allowedTools", tools,
                    "--strict-mcp-config", "--setting-sources", "",
                    "--system-prompt", SCOPED_PROMPT.format(today=today) + extra]
        elif tools and self.computer_mode != "off":
            # only the pet's PC tools (no Gmail/Docs connectors), loaded up
            # front (ENABLE_TOOL_SEARCH off in run_claude): one round trip less
            cmd += ["--tools", PC_TOOLS, "--mcp-config", self.mcp_config(),
                    "--strict-mcp-config", "--allowedTools", "mcp__pc",
                    "--append-system-prompt", f"{PET_PROMPT} {computer.PROMPT}{extra}"]
        elif tools:
            cmd += ["--tools", PET_TOOLS, "--append-system-prompt", PET_PROMPT + extra]
        else:
            today = time.strftime("%Y-%m-%d")
            cmd += ["--tools", "", "--strict-mcp-config", "--setting-sources", "",
                    "--system-prompt", LITE_PROMPT.format(today=today) + extra]
        if session:
            cmd += ["--resume", session]
        return cmd

    def run_claude(self, prompt: str, tools: bool | str, session: str | None,
                   live: bool = True) -> tuple[dict | None, str | None]:
        """One streaming `claude -p` call. Returns (data, None) or (None, error).

        Reply text is pushed to the bubble as it arrives (on_stream) and each
        tool use shows a caption (on_tool). In the lite route the text is
        held back while it could still be the NEED_TOOLS escalation marker.
        """
        try:
            proc = self.claude_proc = subprocess.Popen(self.claude_cmd(tools, session),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace",
                                    cwd=WORK_DIR, creationflags=CREATE_NO_WINDOW,
                                    env={**os.environ, "CLAUDE_PET_CHILD": "1",
                                         **({"ENABLE_TOOL_SEARCH": "false"}
                                            if self.computer_mode != "off" else {})})
        except FileNotFoundError:
            return None, "Claude Code CLI not found. Is `claude` on PATH?"
        timed_out = threading.Event()
        timer = threading.Timer(300, lambda: (timed_out.set(), proc.kill()))
        timer.start()
        err: list[str] = []
        drain = threading.Thread(target=lambda: err.append(proc.stderr.read()),
                                 daemon=True)
        drain.start()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass
        data: dict = {}
        text = ""
        context = 0
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "result":
                    data = msg
                elif kind == "stream_event":
                    ev = msg.get("event", {})
                    etype = ev.get("type")
                    if etype == "message_start":
                        text = ""
                    elif etype == "content_block_start":
                        block = ev.get("content_block", {})
                        if block.get("type") == "tool_use":
                            plan = text.strip()
                            if live and plan:  # "Okay, opening WhatsApp…": say it first
                                self.post(lambda p=plan: self.say_plan(p))
                            text = ""  # pre-tool chatter is not the answer
                            cap = tool_caption(block.get("name", ""), {})
                            if live:
                                self.post(lambda c=cap: self.on_tool(c))
                    elif etype == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text += delta.get("text", "")
                            held = text.strip().rstrip(".")
                            if live and not ESCALATE.startswith(held):
                                self.post(lambda t=text: self.on_stream(t))
                elif kind == "assistant":
                    u = msg.get("message", {}).get("usage") or {}
                    if u:
                        context = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                                   + u.get("cache_creation_input_tokens", 0)
                                   + u.get("cache_read_input_tokens", 0))
                    for block in msg.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":  # now with inputs
                            cap = tool_caption(block.get("name", ""),
                                               block.get("input") or {})
                            if live:
                                self.post(lambda c=cap: self.on_tool(c))
            proc.wait()
        finally:
            timer.cancel()
        drain.join(timeout=2)
        if timed_out.is_set():
            return None, "Timed out after 5 minutes."
        if data.get("is_error") or not data.get("result"):
            stderr = (err[0] if err else "").strip()
            return None, data.get("result") or stderr or "No reply."
        data["pet_usage"] = claude_usage(data, context)
        return data, None

    def run_model(self, prompt: str, tools: bool | str, session: str | None,
                  live: bool = True) -> tuple[dict | None, str | None]:
        """run_claude, or the same request on the chosen provider."""
        if self.provider == "claude":
            return self.run_claude(prompt, tools, session, live)
        web = isinstance(tools, str) and "WebSearch" in tools
        return self.run_other(prompt, False if isinstance(tools, str) else tools,
                              web, session, None, live)

    def run_other(self, prompt: str, tools: bool, web: bool, session: str | None,
                  media: dict | None, live: bool = True) -> tuple[dict | None, str | None]:
        """One request on Codex, Antigravity, Groq or the Gemini API."""
        todo = self.tasks.summary()
        system = SCOPED_PROMPT.format(today=time.strftime("%Y-%m-%d"))
        if todo:
            system += f" The user's open to-do tasks: {todo}."
        system += self.hack_context()
        if tools and self.provider in ("codex", "antigravity"):
            system += " Use your tools (shell, files, web) only when needed."
        media = media or {}
        run = providers.Run(
            self.provider, prompt, system, session, self.effort, web, bool(tools),
            WORK_DIR, images=media.get("images"), files=media.get("files"),
            on_text=(lambda t: self.post(lambda: self.on_stream(t))) if live else None,
            on_tool=(lambda c: self.post(lambda: self.on_tool(c))) if live else None,
            groq_model=self.groq_model_var.get(),
            gemini_model=self.state.get("gemini_model", ""),
            agy_models=(self.account or {}).get("models"))
        reply, new_session, error = run.run()
        if error or reply is None:
            return None, error or "No reply."
        return {"result": reply, "session_id": new_session, "pet_usage": run.usage}, None

    def hack_context(self) -> str:
        event = self.hack_event(hackmode.active_id(self.hack_data))
        if not event:
            return ""
        return hackmode.chat_context(event, self.hack_ws(event), hackathons.utc_now())

    def one_shot_fn(self) -> Callable[[str, str], str | None] | None:
        """ask_fn for projects.pick_top3 when not on Claude Code."""
        if self.provider == "claude":
            return None
        provider, groq = self.provider, self.groq_model_var.get()
        gemini = self.state.get("gemini_model", "")
        return lambda system, prompt: providers.one_shot(provider, system, prompt,
                                                         "low", groq, gemini)

    def ask(self, prompt: str, voice: bool, scoped: bool | str | None = None,
            media: dict | None = None, gen: int = 0) -> None:
        def done(data: dict | None, error: str | None) -> None:
            if gen and gen == self.stopped_gen:
                return  # the user stopped it; work_stopped already showed that
            self.post(lambda: self.finish(data, error, voice))

        if self.chat.wants_auto_compact() and not self.compacting:
            self.post(lambda: setattr(self, "busy_label", "Compacting"))
            self.compact_now(auto=True)
        if self.provider != "claude":
            if scoped is not None:
                tools = False
                web = isinstance(scoped, str) and "WebSearch" in scoped
            else:
                tools = self.tools_var.get() or bool(KEYWORDS.search(prompt))
                if prompt.startswith(FORCE_PREFIX):
                    tools, prompt = True, prompt[len(FORCE_PREFIX):]
                web = tools
            if tools:
                self.post(lambda: setattr(self, "busy_label", "Using tools"))
            session = (self.session_id if providers.session_provider(self.session_id)
                       == self.provider else None)
            sent = prompt if session else self.carry(prompt)
            data, error = self.run_other(sent, tools, web, session, media)
            if data:
                data["used_tools"] = bool(tools)
            done(data, error)
            return
        if scoped is not None:
            tools: bool | str = scoped
        else:
            tools = (self.tools_var.get() or bool(KEYWORDS.search(prompt))
                     or (self.computer_mode != "off" and bool(COMPUTER_WORDS.search(prompt))))
            if prompt.startswith(FORCE_PREFIX):
                tools, prompt = True, prompt[len(FORCE_PREFIX):]
        if tools is True:
            self.post(lambda: setattr(self, "busy_label", "Using tools"))
        pc = self.computer_mode != "off"
        session = self.session_id
        sent = prompt
        if tools is True and pc:
            # A chat that never had the PC tools remembers "I can't reach the
            # PC" and Haiku sticks to that, so it starts fresh instead.
            if session and session != self.state.get("pc_session"):
                session = None
            sent = PC_ON_PREFIX + prompt
        elif tools and session:
            sent = TOOLS_ON_PREFIX + prompt
        if not session:
            sent = self.carry(sent)
        data, error = self.run_claude(sent, tools, session)
        first = (data or {}).get("pet_usage") or {}
        # lite route asks for tools; a resumed lite chat can also make Haiku
        # answer the marker in tools mode, so nudge it on in both cases
        if data and data["result"].strip().rstrip(".") == ESCALATE \
                and gen != self.stopped_gen:
            self.post(lambda: setattr(self, "busy_label", "Using tools"))
            tools = True
            if pc and data["session_id"] != self.state.get("pc_session"):
                # old chat without PC tools: ask again in a fresh one
                data, error = self.run_claude(self.carry(PC_ON_PREFIX + prompt),
                                              True, None)
            else:
                nudge = PC_ON_PREFIX + ESCALATE_NUDGE if pc else ESCALATE_NUDGE
                data, error = self.run_claude(nudge, True, data["session_id"])
        if data and first and data.get("pet_usage") is not first:  # escalated: 2 calls
            u = data["pet_usage"]
            data["pet_usage"] = {**u, "in": u["in"] + first["in"],
                                 "out": u["out"] + first["out"]}
        if data and tools is True and pc:
            self.state["pc_session"] = data.get("session_id")  # saved by finish
        if data:
            data["used_tools"] = bool(tools)
        done(data, error)

    def stop_work(self) -> bool:
        """Interrupt the running request (any thread is fine for the kill).
        Returns True if something was running."""
        self.stop_speaking()
        if not self.busy:
            return False
        self.stopped_gen = self.work_gen
        computer.CANCEL.set()
        proc = self.claude_proc
        if proc and proc.poll() is None:
            subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        self.busy = False
        self.stream_view = False
        self.stream_pending = None
        self.append("info", "Stopped.\n")
        self.tool_tag.configure(text="✋ stopped")
        self.set_bubble("Stopped ✋")
        self.show_bubble()
        self.start_shake(0.3)
        return True

    # ---------- continuous voice ----------
    def on_cont_toggle(self) -> None:
        if self.cont_var.get():
            self.start_continuous()
        else:
            self.stop_continuous()

    def toggle_continuous(self) -> None:
        self.cont_var.set(not self.cont_var.get())
        self.on_cont_toggle()

    def start_continuous(self) -> None:
        if self.cont:
            return
        if LiveListener is None:
            self.cont_var.set(False)
            self.status("Voice needs the pet's .venv (faster-whisper, sounddevice).",
                        "error")
            return
        if self.listening:
            self.cancel_listening()
        self.cont_gen += 1
        gen = self.cont_gen
        self.cont_final, self.cont_used, self.cont_interrupted = "", 0, False
        self.cont = LiveListener(
            on_text=lambda f, p: self.post(lambda: self.cont_heard(gen, f, p)),
            on_error=lambda m: self.post(lambda: self.cont_error(gen, m)),
            on_ready=lambda: self.post(lambda: gen == self.cont_gen and self.status(
                "🎙 Listening all the time. Say a command; it runs after a short "
                "pause. Say anything (or “stop”) while I work and I stop. "
                "Ctrl+Alt+L turns this off.")),
            on_done=lambda: None,
            on_phrase=lambda: self.post(lambda: self.cont_phrase(gen)))
        self.cont.start()
        self.status("🎙 Starting the mic…")

    def stop_continuous(self) -> None:
        self.cont_var.set(False)
        if self.cont:
            self.cont.stop()
            self.cont = None
        self.cont_gen += 1
        if self.cont_job:
            self.root.after_cancel(self.cont_job)
            self.cont_job = None
        self.tool_tag.configure(text="")

    def cont_error(self, gen: int, message: str) -> None:
        if gen == self.cont_gen:
            self.stop_continuous()
            self.status(message, "error")

    def cont_heard(self, gen: int, final: str, partial: str) -> None:
        if gen != self.cont_gen:
            return
        if self.is_speaking() or time.time() < self.echo_until:
            # the pet's own voice is not a command, but a plain "stop" still works
            heard = (final[self.cont_used:] + " " + partial).strip()
            if STOP_WORDS.match(heard):
                self.cont_interrupted = self.stop_work() or self.cont_interrupted
            if self.is_speaking():
                self.echo_until = time.time() + 1.2  # room echo / last words
            if not partial:
                self.cont_final, self.cont_used = final, len(final)
            return
        self.cont_final = final
        if partial:
            if self.cont_job:  # still talking: hold the send
                self.root.after_cancel(self.cont_job)
                self.cont_job = None
            if self.busy and STOP_IN_PARTIAL.search(partial):
                self.cont_interrupted = self.stop_work() or self.cont_interrupted
            if not self.busy:
                self.tool_tag.configure(text="🎙 " + partial[-40:])

    def cont_phrase(self, gen: int) -> None:
        """A pause in speech: stop running work at once, send after a beat."""
        if gen != self.cont_gen:
            return
        new = self.cont_final[self.cont_used:].strip()
        if self.clean_phrase(new) in NOISE_PHRASES:
            return
        if self.busy:
            self.cont_interrupted = self.stop_work() or self.cont_interrupted
        if self.cont_job:
            self.root.after_cancel(self.cont_job)
        self.cont_job = self.root.after(CONT_SEND_MS, lambda: self.cont_send(gen))

    @staticmethod
    def clean_phrase(text: str) -> str:
        return re.sub(r"[^\w\s']", "", text).strip().lower()

    def cont_send(self, gen: int) -> None:
        self.cont_job = None
        if gen != self.cont_gen:
            return
        if self.busy:  # the stopped request is still winding down
            self.cont_job = self.root.after(200, lambda: self.cont_send(gen))
            return
        text = self.cont_final[self.cont_used:].strip()
        self.cont_used = len(self.cont_final)
        self.tool_tag.configure(text="")
        if self.clean_phrase(text) in NOISE_PHRASES or STOP_WORDS.match(text):
            return  # just "stop": already stopped above
        prompt = text
        if self.cont_interrupted:
            prompt = ("(I stopped you mid-task. Drop what you were doing unless "
                      "I say to continue it.) " + text)
        self.cont_interrupted = False
        self.send(prompt, voice=True, shown="🎙 " + text)

    def say_plan(self, plan: str) -> None:
        """Speak what the AI is about to do, once per request, before it acts."""
        if self.busy and self.talk_back and not self.spoke_plan:
            self.spoke_plan = True
            self.speak(plan)

    def computer_step(self, summary: str) -> None:
        """A computer tool is starting: caption it, and if the AI said nothing
        yet, say the action itself ("Open whatsapp")."""
        self.on_tool(summary)
        self.say_plan(summary)

    def finish(self, data: dict | None, error: str | None, voice: bool) -> None:
        self.busy = False
        self.usage_now.set()
        self.last_active = time.time()
        took = time.time() - self.busy_since
        if error or data is None:
            self.stream_view = False
            self.stream_pending = None
            self.append("error", f"{error}\n")
            self.tool_tag.configure(text="")
            self.set_bubble(str(error), "error")
            self.start_shake()
            if self.provider == "claude" and account.is_auth_error(str(error)):
                self.account = {**(self.account or {}), "installed": True,
                                "linked": False}  # login missing or expired
                self.root.after(1500, self.show_link_panel)
                self.check_link(quiet=True)
            elif self.provider != "claude" and re.search(
                    r"\b40[13]\b|log ?in|api key|not installed", str(error), re.I):
                self.check_link()
        else:
            self.session_id = data.get("session_id", self.session_id)
            self.state["session_id"] = self.session_id
            save_state(self.state)
            self.last_reply = data["result"].strip()
            used = data.get("pet_usage") or {}
            self.chat.add_usage(used)
            self.chat.remember(self.pending_prompt, self.last_reply)
            self.append("claude", self.last_reply + "\n",
                        who=self.provider_name() + (" 🔧" if data.get("used_tools") else ""))
            self.set_bubble(self.last_reply, markdown=True,
                            reveal=not self.stream_view)
            self.stream_view = False
            self.stream_pending = None
            meta = f"{took:.1f}s"
            if used.get("in") or used.get("out"):
                meta += (f" · ↑{chatlog.short(used.get('in') or 0)}"
                         f" ↓{chatlog.short(used.get('out') or 0)}")
            if data.get("used_tools"):
                meta = "🔧 tools · " + meta
            self.tool_tag.configure(text=meta)
            self.update_ctx_label()
            self.happy_until = time.time() + 2.5
            self.start_hop(0.42, 6 * ZOOM)
            if voice or self.speak_var.get():
                self.speak_after(self.last_reply, self.work_gen)
        if self.fx.shown(self.bar):
            self.show_bubble()
        else:
            self.fx.hide(self.bubble)
            self.unread = True

    # ---------- voice: live speech to text ----------
    # Spoken words stream into the ask box as you talk. Nothing is sent
    # until you press Enter, so you can review or edit the text first.
    def talk(self) -> None:
        """Open the box and start listening (global hotkey / menu)."""
        if not self.fx.shown(self.bar):
            self.show_bar()
        if not self.listening:
            self.toggle_listen()

    def toggle_listen(self) -> None:
        if self.listening:
            self.stop_listening()
            return
        if self.finishing:
            return
        if self.cont:  # the box mic and continuous voice share the mic
            self.stop_continuous()
        if LiveListener is None:
            self.status("Voice needs the pet's .venv (faster-whisper, sounddevice).",
                        "error")
            return
        self.stop_speaking()
        self.listening = True
        self.voice_used = True
        self.voice_base = self.entry_text().rstrip()
        self.listen_gen += 1
        gen = self.listen_gen
        self.mic_btn.configure(fg=RED)
        self.placeholder.configure(text="Starting mic…")
        self.listener = LiveListener(
            on_text=lambda f, p: self.post(lambda: self.show_heard(gen, f, p)),
            on_error=lambda m: self.post(lambda: self.listen_error(gen, m)),
            on_ready=lambda: self.post(lambda: self.mic_ready(gen)),
            on_done=lambda: self.post(lambda: self.listen_done(gen)),
            on_phrase=(lambda: self.post(lambda: self.wake_phrase(gen)))
            if self.wake_session else None)
        self.listener.start()

    def mic_ready(self, gen: int) -> None:
        if gen == self.listen_gen and self.listening:
            self.placeholder.configure(text="Listening… speak now")

    def show_heard(self, gen: int, final: str, partial: str) -> None:
        if gen != self.listen_gen:
            return
        text = " ".join(t for t in (self.voice_base, final, partial) if t)
        self.set_entry(text)

    def listen_error(self, gen: int, message: str) -> None:
        if gen == self.listen_gen:
            self.send_pending = False
            self.status(message, "error")
            self.start_shake()

    def stop_listening(self) -> None:
        """Stop the mic; the last words are transcribed, then listen_done runs."""
        if self.listener:
            self.listener.stop()
            self.listener = None
            self.finishing = True
            self.placeholder.configure(text="Finishing…")
        self.listening = False
        self.mic_btn.configure(fg=MUTED)

    def cancel_listening(self) -> None:
        """Stop the mic and ignore anything still being transcribed."""
        self.stop_listening()
        self.listen_gen += 1
        self.finishing = False
        self.send_pending = False
        self.wake_session = False
        self.placeholder.configure(text=f"Ask {self.provider_name()}…")

    def listen_done(self, gen: int) -> None:
        if gen != self.listen_gen:
            return
        self.listening = False
        self.finishing = False
        self.listener = None
        self.wake_session = False
        self.mic_btn.configure(fg=MUTED)
        self.placeholder.configure(text=f"Ask {self.provider_name()}…")
        self.update_placeholder()
        if self.send_pending:  # Enter was pressed while still listening
            self.send_pending = False
            self.on_enter(None)

    # ---------- voice: text to speech ----------
    def is_speaking(self) -> bool:
        return self.speak_proc is not None and self.speak_proc.poll() is None

    def speak(self, text: str) -> None:
        self.stop_speaking()
        try:
            self.speak_proc = subprocess.Popen(
                POWERSHELL + [str(SPEAK_SCRIPT)], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
            assert self.speak_proc.stdin is not None
            self.speak_proc.stdin.write(speech_text(text).encode("utf-8"))
            self.speak_proc.stdin.close()
        except OSError as exc:
            self.status(f"Speech failed: {exc}", "error")

    def speak_after(self, text: str, gen: int) -> None:
        """Speak once the current sentence (the spoken plan) is done, unless a
        new request started meanwhile."""
        if gen != self.work_gen:
            return
        if self.is_speaking():
            self.root.after(150, lambda: self.speak_after(text, gen))
            return
        self.speak(text)

    def stop_speaking(self) -> None:
        if self.is_speaking():
            assert self.speak_proc is not None
            self.speak_proc.kill()
        self.speak_proc = None

    # ---------- global hotkeys ----------
    def start_hotkeys(self) -> None:
        threading.Thread(target=self.hotkey_loop, daemon=True).start()

    def hotkey_loop(self) -> None:
        user32 = ctypes.windll.user32
        mods = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        actions = {HOTKEY_TOGGLE: self.hotkey_space, HOTKEY_TALK: self.talk,
                   HOTKEY_SHOT: self.snip, HOTKEY_CONT: self.toggle_continuous}
        user32.RegisterHotKey(None, HOTKEY_TOGGLE, mods, VK_SPACE)
        user32.RegisterHotKey(None, HOTKEY_TALK, mods, VK_V)
        user32.RegisterHotKey(None, HOTKEY_SHOT, mods, VK_S)
        user32.RegisterHotKey(None, HOTKEY_CONT, mods, VK_L)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in actions:
                self.post(actions[msg.wParam])

    def hotkey_space(self) -> None:
        """Ctrl+Alt+Space: tap to show or hide the box, hold to talk.

        The box appears on key down, so a tap feels instant. A short press
        on an already-open box hides it; holding for HOLD_TO_TALK seconds
        starts listening instead.
        """
        if getattr(self, "space_held", False):
            return
        self.space_held = True
        was_shown = self.fx.shown(self.bar)
        if not was_shown:
            self.show_bar()
        typed_before = self.entry_text()

        def watch() -> None:
            key = ctypes.windll.user32.GetAsyncKeyState
            start = time.time()
            while key(VK_SPACE) & 0x8000:
                if time.time() - start >= HOLD_TO_TALK:
                    self.post(lambda: self.space_done(True, was_shown, typed_before))
                    return
                time.sleep(0.03)
            self.post(lambda: self.space_done(False, was_shown, typed_before))

        threading.Thread(target=watch, daemon=True).start()

    def space_done(self, held: bool, was_shown: bool, typed_before: str) -> None:
        self.space_held = False
        # Key repeats while holding can leak spaces into the box; drop them.
        now = self.entry_text()
        if now != typed_before and now.startswith(typed_before) \
                and not now[len(typed_before):].strip():
            self.set_entry(typed_before)
        if held:
            self.talk()
        elif was_shown:
            self.hide_all()

    def quit(self) -> None:
        self.count_keys(save=True)
        self.key_counter.stop()
        self.stop_continuous()
        if self.wake:
            self.wake.stop()
        self.cancel_listening()
        self.stop_speaking()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    os.chdir(WORK_DIR)
    ClaudePet().run()
