"""AI providers the pet can run on, besides Claude Code.

The pet never pays for anything itself. Each provider uses the person's own
account or key:

- "claude"     Claude Code CLI (Claude plan or Console credits). Handled in
               claude_pet.py / account.py; listed here for menus and status.
- "codex"      OpenAI Codex CLI, signed in with ChatGPT (plan credits) or an
               OpenAI API key. Runs `codex exec --json`.
- "antigravity" Google's Antigravity CLI (`agy`), signed in with the Google
               account / AI plan Antigravity uses. Runs `agy -p --output-format
               stream-json`. (Gemini CLI stopped serving personal Google
               accounts on 18 June 2026; agy replaces it.)
- "groq"       Groq API key, gpt-oss-120b or gpt-oss-20b.
- "gemini"     Gemini API key (Google AI Studio).

API keys live in the pet's `.env` file (GROQ_API_KEY, GEMINI_API_KEY),
never in code. Chats for providers without their own sessions (Groq, Gemini
API) are kept as short local histories in ~/.claude-pet-chats.
"""

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

import usage

from pet_paths import APP_DIR, ENV_FILE  # noqa: E402
CHATS_DIR = Path.home() / ".claude-pet-chats"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
TIMEOUT_SECONDS = 300
HISTORY_TURNS = 12  # user+assistant pairs kept for local chats
FILE_CHARS = 20_000  # text read from each attached file for API providers

PROVIDERS = {
    "claude": "Claude Code",
    "codex": "Codex (ChatGPT)",
    "antigravity": "Antigravity (Google account)",
    "groq": "Groq API key",
    "gemini": "Gemini API key",
}
SHORT = {"claude": "Claude", "codex": "Codex", "antigravity": "Gemini",
         "groq": "Groq", "gemini": "Gemini"}
KEY_NAMES = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}
KEY_PAGES = {"groq": "https://console.groq.com/keys",
             "gemini": "https://aistudio.google.com/apikey"}
GROQ_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b")
GROQ_URL = "https://api.groq.com/openai/v1"
CITATION = re.compile(r"\s*【[^】]*】")  # gpt-oss browser_search source marks
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
INSTALL = {"codex": "npm install -g @openai/codex",
           "antigravity": "winget install --id Google.AntigravityCLI -e"}
# Antigravity model per pet effort: newest Flash at that thinking level.
AGY_LEVEL = {"low": "low", "medium": "medium", "high": "high",
             "xhigh": "high", "max": "high"}
# Pet effort levels mapped onto what each provider accepts.
CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high",
                "xhigh": "xhigh", "max": "xhigh"}
GROQ_EFFORT = {"low": "low", "medium": "medium", "high": "high",
               "xhigh": "high", "max": "high"}

OnText = Callable[[str], None]
OnTool = Callable[[str], None]


# ---------- keys (.env) ----------
def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def get_key(provider: str) -> str:
    name = KEY_NAMES[provider]
    return read_env().get(name) or os.environ.get(name, "")


def set_key(provider: str, value: str) -> None:
    """Save (or with "" remove) a provider's key in the pet's .env file."""
    name = KEY_NAMES[provider]
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lines = [ln for ln in lines if not ln.strip().startswith(name + "=")]
    if value:
        lines.append(f"{name}={value}")
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mask(key: str) -> str:
    return f"…{key[-4:]}" if len(key) > 8 else "set"


# ---------- HTTP ----------
def _request(url: str, headers: dict, body: dict | None = None,
             timeout: int = 30) -> urllib.request.Request:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    for k, v in {"Content-Type": "application/json",
                 "User-Agent": "claude-pet", **headers}.items():
        req.add_header(k, v)
    return req


def _http_error(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(exc.read().decode("utf-8", "replace"))
        err = detail.get("error", detail)
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, OSError):
        msg = exc.reason
    return f"HTTP {exc.code}: {msg}"


def _sse(resp) -> "iter[dict]":
    """Yield JSON payloads from a server-sent-events response."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except ValueError:
            continue


def gemini_headers(key: str) -> dict:
    return {"x-goog-api-key": key}


def check_key(provider: str, key: str) -> tuple[bool, str]:
    """Validate a key with a free model-list call. Returns (ok, detail).

    For Gemini, `detail` on success is the best Flash model the key can use.
    """
    if provider == "groq":
        req = _request(f"{GROQ_URL}/models", {"Authorization": f"Bearer {key}"})
    else:
        req = _request(f"{GEMINI_URL}/models?pageSize=200", gemini_headers(key))
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return False, _http_error(exc)
    except (OSError, ValueError) as exc:
        return False, f"Could not reach the API: {exc}"
    if provider == "groq":
        ids = {m.get("id") for m in data.get("data", [])}
        if not ids & set(GROQ_MODELS):
            return False, "This key can't use gpt-oss models."
        return True, ""
    model = pick_gemini_model(data.get("models", []))
    return (True, model) if model else (False, "No Gemini Flash model on this key.")


def pick_gemini_model(models: list[dict]) -> str:
    """Newest stable Flash model that can chat (falls back to previews)."""
    best: tuple = ()
    for m in models:
        name = m.get("name", "").removeprefix("models/")
        if ("generateContent" not in m.get("supportedGenerationMethods", [])
                or "flash" not in name or re.search(
                    r"image|tts|audio|live|embed|thinking|exp|8b|lite", name)):
            continue
        ver = [int(x) for x in re.findall(r"\d+", name.split("flash")[0])]
        stable = "preview" not in name and not re.search(r"-\d{2}-\d{2}|-\d{3}$", name)
        rank = (stable, ver, name == f"gemini-{'.'.join(map(str, ver))}-flash")
        if not best or rank > best[0]:
            best = (rank, name)
    return best[1] if best else ""


# ---------- status ----------
def which(exe: str) -> str | None:
    return shutil.which(exe)


def find_agy() -> str | None:
    """Antigravity CLI path (winget link, or the PowerShell installer's folder)."""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for path in (shutil.which("agy"), local / "Microsoft" / "WinGet" / "Links" / "agy.exe",
                 local / "agy" / "bin" / "agy.exe"):
        if path and Path(path).exists():
            return str(path)
    return None


def pick_agy_model(models: list[str], effort: str) -> str:
    """Newest Gemini Flash model at the effort's thinking level ("" = CLI default)."""
    level = AGY_LEVEL.get(effort, "low")
    flash = [m for m in models if re.fullmatch(rf"gemini-[\d.]+-flash-{level}", m)]
    ver = lambda m: [int(x) for x in re.findall(r"\d+", m.split("-flash")[0])]  # noqa: E731
    return max(flash, key=ver) if flash else ""


def status(provider: str) -> dict:
    """{"installed", "linked", "who"} for a non-Claude provider."""
    if provider in KEY_NAMES:
        key = get_key(provider)
        return {"installed": True, "linked": bool(key),
                "who": f"key {mask(key)}" if key else ""}
    if provider == "codex":
        exe = which("codex")
        if not exe:
            return {"installed": False, "linked": False, "who": ""}
        try:
            out = subprocess.run([exe, "login", "status"], capture_output=True,
                                 text=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            text = (out.stdout + out.stderr).strip()
        except (OSError, subprocess.TimeoutExpired):
            return {"installed": True, "linked": False, "who": ""}
        ok = out.returncode == 0 and "logged in" in text.lower()
        who = text.replace("Logged in using ", "").strip() if ok else ""
        return {"installed": True, "linked": ok, "who": who}
    if provider == "antigravity":
        exe = find_agy()
        if not exe:
            return {"installed": False, "linked": False, "who": ""}
        try:  # listing models needs a signed-in account
            out = subprocess.run([exe, "models"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60,
                                 stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return {"installed": True, "linked": False, "who": ""}
        models = [ln.split("\t")[0] for ln in out.stdout.splitlines() if "\t" in ln]
        return {"installed": True, "linked": out.returncode == 0 and bool(models),
                "who": "Google account", "models": models}
    return {"installed": False, "linked": False, "who": ""}


def open_terminal(command: str) -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    subprocess.Popen([shell, "-NoExit", "-Command", command],
                     creationflags=CREATE_NEW_CONSOLE)


def install(provider: str) -> None:
    open_terminal(INSTALL[provider])


def login(provider: str) -> None:
    """Open the provider's own sign-in in a terminal."""
    if provider == "codex":
        open_terminal("codex login")
    elif provider == "antigravity":
        exe = find_agy() or "agy"
        open_terminal(f"Write-Host 'Sign in with Google in the browser, then type /quit.'; "
                      f"& '{exe}'")


def logout(provider: str) -> None:
    if provider == "codex" and which("codex"):
        subprocess.run([which("codex"), "logout"], capture_output=True,
                       timeout=30, creationflags=CREATE_NO_WINDOW)


# ---------- local chat history ----------
def new_session(provider: str) -> str:
    return f"{provider}:{uuid.uuid4()}"


def session_provider(session: str | None) -> str:
    """Which provider a pet session id belongs to (plain UUIDs are Claude)."""
    if session and ":" in session:
        return session.split(":", 1)[0]
    return "claude"


def _chat_file(session: str) -> Path:
    return CHATS_DIR / (re.sub(r"[^\w-]", "_", session) + ".json")


def load_chat(session: str | None) -> list[dict]:
    if not session:
        return []
    try:
        return json.loads(_chat_file(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_chat(session: str, history: list[dict]) -> None:
    CHATS_DIR.mkdir(exist_ok=True)
    _chat_file(session).write_text(
        json.dumps(history[-2 * HISTORY_TURNS:]), encoding="utf-8")


def _file_block(paths: list[str]) -> str:
    parts = []
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")[:FILE_CHARS]
        except OSError as exc:
            text = f"(could not read: {exc})"
        parts.append(f'<file path="{p}">\n{text}\n</file>')
    return "\n".join(parts)


# ---------- runners ----------
class Run:
    """One request. `run()` returns (reply, session, error)."""

    def __init__(self, provider: str, prompt: str, system: str, session: str | None,
                 effort: str, web: bool, tools: bool, cwd: Path,
                 images: list[str] | None = None, files: list[str] | None = None,
                 on_text: OnText | None = None, on_tool: OnTool | None = None,
                 groq_model: str = GROQ_MODELS[0], gemini_model: str = "",
                 agy_models: list[str] | None = None) -> None:
        self.provider, self.prompt, self.system = provider, prompt, system
        self.session = session if session_provider(session) == provider else None
        self.effort, self.web, self.tools, self.cwd = effort, web, tools, cwd
        self.images, self.files = images or [], files or []
        self.on_text = on_text or (lambda _t: None)
        self.on_tool = on_tool or (lambda _t: None)
        self.groq_model, self.gemini_model = groq_model, gemini_model
        self.agy_models = agy_models or []
        # tokens of this request: {"in", "out", "context", "window"}
        self.usage: dict = {}

    def run(self) -> tuple[str | None, str | None, str | None]:
        try:
            if self.provider == "codex":
                return self.codex()
            if self.provider == "antigravity":
                return self.antigravity()
            if self.provider == "groq":
                return self.groq()
            if self.provider == "gemini":
                return self.gemini_api()
        except urllib.error.HTTPError as exc:
            return None, None, _http_error(exc)
        except (OSError, ValueError) as exc:
            return None, None, f"{PROVIDERS[self.provider]} failed: {exc}"
        return None, None, f"Unknown provider {self.provider}"

    # --- CLIs ---
    def _stream_cli(self, cmd: list[str], stdin: str | None,
                    handle: Callable[[dict], None]) -> tuple[int, str]:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", cwd=self.cwd,
                                creationflags=CREATE_NO_WINDOW)
        timer = threading.Timer(TIMEOUT_SECONDS, proc.kill)
        timer.start()
        err: list[str] = []
        drain = threading.Thread(target=lambda: err.append(proc.stderr.read()),
                                 daemon=True)
        drain.start()
        try:
            if stdin is not None:
                proc.stdin.write(stdin)
                proc.stdin.close()
        except OSError:
            pass
        try:
            for line in proc.stdout:
                try:
                    handle(json.loads(line))
                except ValueError:
                    continue
            proc.wait()
        finally:
            timer.cancel()
        drain.join(timeout=2)
        return proc.returncode, (err[0] if err else "").strip()

    def codex(self) -> tuple[str | None, str | None, str | None]:
        exe = which("codex")
        if not exe:
            return None, None, "Codex CLI is not installed."
        thread = self.session.split(":", 1)[1] if self.session else None
        sandbox = "workspace-write" if self.tools else "read-only"
        opts = ["--json", "--skip-git-repo-check",
                "-c", f'sandbox_mode="{sandbox}"',
                "-c", f'model_reasoning_effort="{CODEX_EFFORT.get(self.effort, "low")}"']
        if self.web or self.tools:
            opts += ["-c", 'web_search="live"']
        for img in self.images:
            opts += ["-i", img]
        cmd = [exe, "exec"] + (["resume", thread] if thread else []) + opts + ["-"]
        text = self.prompt if thread else f"{self.system}\n\n{self.prompt}"
        state = {"reply": "", "thread": thread, "error": ""}

        def handle(ev: dict) -> None:
            kind = ev.get("type")
            item = ev.get("item", {})
            if kind == "thread.started":
                state["thread"] = ev.get("thread_id")
            elif kind == "item.started" and item.get("type") in (
                    "command_execution", "web_search", "file_change"):
                self.on_tool({"command_execution": "Running a command…",
                              "web_search": "Searching the web…",
                              "file_change": "Editing files…"}[item["type"]])
            elif kind == "item.completed" and item.get("type") == "agent_message":
                state["reply"] = item.get("text", "")
                self.on_text(state["reply"])
            elif kind == "turn.completed":
                used = ev.get("usage") or {}
                self.usage = {"in": used.get("input_tokens", 0),
                              "out": used.get("output_tokens", 0),
                              "context": used.get("input_tokens", 0)
                              + used.get("output_tokens", 0)}
            elif kind in ("error", "turn.failed"):
                state["error"] = (ev.get("message")
                                  or ev.get("error", {}).get("message", "")) or "Codex error"

        code, stderr = self._stream_cli(cmd, text, handle)
        if state["reply"]:
            return state["reply"], f"codex:{state['thread']}", None
        return None, None, state["error"] or stderr[-400:] or f"Codex exited {code}"

    def antigravity(self) -> tuple[str | None, str | None, str | None]:
        exe = find_agy()
        if not exe:
            return None, None, "Antigravity CLI is not installed."
        conv = self.session.split(":", 1)[1] if self.session else None
        attach = "".join(f"\nAttached file: {p}" for p in self.images + self.files)
        text = (self.prompt if conv else f"{self.system}\n\n{self.prompt}") + attach
        cmd = [exe, "-p", text, "--output-format", "stream-json",
               "--print-timeout", f"{TIMEOUT_SECONDS}s"]
        model = pick_agy_model(self.agy_models, self.effort)
        if model:
            cmd += ["--model", model]
        if conv:
            cmd += ["--conversation", conv]
        if self.tools:  # like Codex's workspace-write: act, but in a sandbox
            cmd += ["--dangerously-skip-permissions", "--sandbox"]
        else:
            cmd += ["--mode", "plan"]
        state = {"reply": "", "step": None, "conv": conv, "error": "", "denied": False}

        def handle(ev: dict) -> None:
            kind = ev.get("event")
            if kind == "init":
                state["conv"] = ev.get("conversation_id") or state["conv"]
            elif kind == "step_update":
                step = ev.get("step_update", {})
                if step.get("step_type") == "agent_response":
                    if step.get("step_index") != state["step"]:  # new answer block
                        state["step"], state["reply"] = step.get("step_index"), ""
                    state["reply"] += step.get("text_delta", "")
                    self.on_text(state["reply"])
                elif step.get("state") == "ACTIVE" and step.get("step_type") not in (
                        "user_input", None):
                    self.on_tool(f"Using {step['step_type'].replace('_', ' ')}…")
            elif kind == "result":
                res = ev.get("result", {})
                state["conv"] = res.get("conversation_id") or state["conv"]
                if res.get("response"):
                    state["reply"] = res["response"]
                state["denied"] = bool(res.get("denied_actions"))
                if res.get("status") not in (None, "SUCCESS"):
                    state["error"] = f"Antigravity: {res.get('status')}"
            elif kind == "error":
                state["error"] = ev.get("message") or str(ev.get("error", "Antigravity error"))

        code, stderr = self._stream_cli(cmd, None, handle)
        usage.record("antigravity")
        reply = state["reply"].strip()
        if reply:
            return reply, f"antigravity:{state['conv']}", None
        if state["denied"]:
            return None, None, ("Antigravity needed to run a tool. Ask again with "
                                "\"/t \" in front to let it use tools.")
        return None, None, state["error"] or stderr[-400:] or f"Antigravity exited {code}"

    # --- APIs ---
    def groq(self) -> tuple[str | None, str | None, str | None]:
        key = get_key("groq")
        if not key:
            return None, None, "No Groq API key set."
        if self.images:
            return None, None, ("gpt-oss on Groq can't see images. Switch to Codex, "
                                "Gemini or Claude Code for screenshots.")
        session = self.session or new_session("groq")
        history = load_chat(session)
        prompt = self.prompt + ("\n\n" + _file_block(self.files) if self.files else "")
        messages = ([{"role": "system", "content": self.system}]
                    + [{"role": "assistant" if m["role"] == "model" else "user",
                        "content": m["text"]} for m in history]
                    + [{"role": "user", "content": prompt}])
        body = {"model": self.groq_model, "messages": messages, "stream": True,
                "reasoning_effort": GROQ_EFFORT.get(self.effort, "low"),
                "stream_options": {"include_usage": True}}
        if self.web:
            body["tools"] = [{"type": "browser_search"}]
            self.on_tool("Searching the web…")
        reply, tokens, headers = "", 0, {}
        for attempt in range(2):
            reply = ""
            req = _request(f"{GROQ_URL}/chat/completions",
                           {"Authorization": f"Bearer {key}"}, body)
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                    headers = resp.headers
                    for ev in _sse(resp):
                        used = ev.get("usage") or (ev.get("x_groq") or {}).get("usage")
                        if used:
                            tokens = used.get("total_tokens", 0)
                            self.usage = {"in": used.get("prompt_tokens", 0),
                                          "out": used.get("completion_tokens", 0),
                                          "context": tokens, "window": 131_072}
                        for ch in ev.get("choices", []):
                            piece = (ch.get("delta") or {}).get("content") or ""
                            if piece:
                                reply += piece
                                self.on_text(CITATION.sub("", reply))
                break
            except urllib.error.HTTPError as exc:
                # Free tier: a few thousand tokens a minute. Wait once if it's short.
                if exc.code != 429:
                    return None, None, _http_error(exc)
                wait = re.search(r"try again in ([\d.]+)s",
                                 exc.read().decode("utf-8", "replace"))
                if attempt or not wait or float(wait.group(1)) > 30:
                    return None, None, ("Groq rate limit reached. Wait a minute, "
                                        "or pick the gpt-oss-20b model.")
                self.on_tool("Groq is busy, retrying…")
                time.sleep(float(wait.group(1)) + 0.5)
        reply = CITATION.sub("", reply)
        try:  # requests-per-day allowance, for the usage meter
            limit = int(headers.get("x-ratelimit-limit-requests") or 0)
            left = int(headers.get("x-ratelimit-remaining-requests") or 0)
        except (TypeError, ValueError):
            limit = left = 0
        usage.record("groq", tokens, limit=limit or None, remaining=left)
        if not reply.strip():
            return None, None, "Groq sent an empty reply."
        save_chat(session, history + [{"role": "user", "text": prompt},
                                      {"role": "model", "text": reply}])
        return reply, session, None

    def gemini_api(self) -> tuple[str | None, str | None, str | None]:
        key = get_key("gemini")
        if not key:
            return None, None, "No Gemini API key set."
        model = self.gemini_model or "gemini-2.5-flash"
        session = self.session or new_session("gemini")
        history = load_chat(session)
        prompt = self.prompt + ("\n\n" + _file_block(self.files) if self.files else "")
        parts: list[dict] = [{"text": prompt}]
        for img in self.images:
            mime = mimetypes.guess_type(img)[0] or "image/png"
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(
                Path(img).read_bytes()).decode()}})
        contents = ([{"role": m["role"], "parts": [{"text": m["text"]}]} for m in history]
                    + [{"role": "user", "parts": parts}])
        body: dict = {"contents": contents,
                      "systemInstruction": {"parts": [{"text": self.system}]}}
        if self.web:
            body["tools"] = [{"google_search": {}}]
            self.on_tool("Searching Google…")
        req = _request(f"{GEMINI_URL}/models/{model}:streamGenerateContent?alt=sse",
                       gemini_headers(key), body)
        reply, tokens = "", 0
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            for ev in _sse(resp):
                meta = ev.get("usageMetadata") or {}
                tokens = meta.get("totalTokenCount", tokens)
                if meta:
                    out = (meta.get("candidatesTokenCount", 0)
                           + meta.get("thoughtsTokenCount", 0))
                    self.usage = {"in": meta.get("promptTokenCount", 0), "out": out,
                                  "context": tokens, "window": 1_048_576}
                for cand in ev.get("candidates", []):
                    for part in (cand.get("content") or {}).get("parts", []):
                        if part.get("text") and not part.get("thought"):
                            reply += part["text"]
                            self.on_text(reply)
        usage.record("gemini", tokens)
        if not reply.strip():
            return None, None, "Gemini sent an empty reply."
        save_chat(session, history + [{"role": "user", "text": prompt},
                                      {"role": "model", "text": reply}])
        return reply, session, None


def one_shot(provider: str, system: str, prompt: str, effort: str = "low",
             groq_model: str = GROQ_MODELS[0], gemini_model: str = "",
             usage_out: dict | None = None) -> str | None:
    """A single stateless request (project picking etc.). None on failure.

    usage_out, when given, is filled with the request's token counts.
    """
    run = Run(provider, prompt, system, None, effort, web=False, tools=False,
              cwd=Path.home(), groq_model=groq_model, gemini_model=gemini_model)
    reply, session, error = run.run()
    if usage_out is not None:
        usage_out.update(run.usage)
    if session and provider != "codex":
        try:
            _chat_file(session).unlink()
        except OSError:
            pass
    return None if error else reply
