"""Project launcher: find a project from a loose description and open it.

The pet keeps an index of the projects under the project roots (name,
folder, a one-line summary from the README or package files, key
dependencies, and how to start the dev server).

Finding a project from a request like "open the whatsapp carpool bot":
1. If the request is just a project's name, that project opens directly.
2. Otherwise a tiny local embedding model (model2vec potion-base-2M, 8 MB,
   a lookup table, so almost no CPU) plus word matching shortlists the 10
   closest projects.
3. Haiku reads only that shortlist and picks the best 3, and the user
   chooses one. Haiku sees ~10 lines instead of the whole catalog.
Launching opens VS Code and, inside its integrated terminal, Claude Code
plus one terminal per dev server. A tiny companion extension (vscode-ext/)
does the terminal part: the pet writes a request file, and the VS Code
window with that folder open claims it. Without the extension, the pet falls
back to Windows Terminal tabs.
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

from pet_paths import MODELS_DIR


def _roots() -> list[Path]:
    """Folders scanned for projects. BOBA_PET_PROJECTS (paths separated by
    ";") wins; otherwise the usual places that exist on this PC."""
    custom = os.environ.get("BOBA_PET_PROJECTS", "")
    if custom.strip():
        return [Path(p).expanduser() for p in custom.split(os.pathsep) if p.strip()]
    home = Path.home()
    found = [p for p in (home / "VS Code" / "PROJECTS", home / "Projects",
                         home / "projects", home / "source" / "repos", home / "code",
                         home / "dev", home / "Documents" / "GitHub", home / "GitHub")
             if p.is_dir()]
    return found or [home]


ROOTS = _roots()
INDEX_FILE = Path.home() / ".claude-pet-projects.json"
INDEX_MAX_AGE = 12 * 3600
MAX_DEPTH = 4
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
EMBED_MODEL = MODELS_DIR / "potion-base-2M"
EMBED_MODEL_ID = "minishlab/potion-base-2M"  # downloaded on first use
SHORTLIST = 10
LEXICAL_WEIGHT = 0.03  # word-match score added to cosine similarity
EXT_DIR = Path(__file__).resolve().parent / "vscode-ext"
VSCODE_REQUEST = Path.home() / ".claude-pet-vscode.json"
CLAUDE_COMMAND = "claude"

MARKERS = {"package.json", "requirements.txt", "pyproject.toml", "setup.py",
           "manage.py", "main.py", "app.py", "index.html", "Cargo.toml", "go.mod",
           "pom.xml", "build.gradle", "Dockerfile", "README.md", "readme.md",
           "hardhat.config.ts", "hardhat.config.js", "CMakeLists.txt", "Makefile"}
MARKER_EXTS = {".sln", ".ipynb", ".csproj", ".cpp", ".java"}
SUBAPPS = ("frontend", "backend", "client", "server", "web", "app", "api",
           "admin", "dashboard", "backend-python", "backend_production")
SKIP = {"node_modules", ".venv", "venv", "env", "__pycache__", ".git", "dist",
        "build", ".next", "target", "site-packages", ".idea", ".vscode", "shots",
        "models", "data", "assets", "public", "static", "images", "img"}
STOPWORDS = {"open", "launch", "start", "run", "load", "resume", "switch", "to",
             "work", "on", "lets", "let", "s", "continue", "with", "go", "jump",
             "into", "fire", "spin", "boot", "up", "the", "my", "a", "an", "that",
             "project", "repo", "codebase", "folder", "please", "can", "you", "for",
             "me", "i", "was", "working", "one", "about", "of", "in", "and", "is",
             "it", "app", "code", "pls", "hey", "where", "made", "built", "we"}

# "open ...", "let's work on ...", "/p ..." -> the part after the verb
LAUNCH_RE = re.compile(
    r"^\s*(?:hey\s+|please\s+|can you\s+|could you\s+|pls\s+)*"
    r"(?:/p\b|/open\b|open(?: up)?|launch|fire up|spin up|boot up|load|resume|"
    r"switch to|let'?s (?:work on|open|start|continue)|work on|continue (?:on|with)|"
    r"jump into|start(?: working on)?)\s+(?P<what>.+)$", re.I)
# a short line that just names a project: "fastverse project"
BARE_RE = re.compile(r"^(?P<what>[\w .'&-]{2,50}?)\s+(?:project|repo|codebase)$", re.I)
NOT_PROJECT = re.compile(
    r"^(?:a |an |the |my )?(?:focus|timer|pomodoro|break|new chat|chat|"
    r"conversation|reminder)\b|https?://|www\.|\.(?:com|org|io|net|pk)\b|"
    r"^(?:the |my )?(?:youtube|gmail|google|chrome|edge|microsoft edge|browser|"
    r"spotify|settings|notepad|calculator|explorer|file explorer|whatsapp|"
    r"word|excel|powerpoint|outlook|teams|discord|telegram|camera|photos|"
    r"task manager|control panel|downloads|desktop|documents)"
    r"(?: browser| app| application| folder)?$", re.I)
# Explicit project requests, used when the pet can operate the PC: then any
# other "open ..." means an app, file or site.
EXPLICIT_PROJECT = re.compile(r"^\s*/(?:p|open)\b|\b(?:project|repo|codebase)\b", re.I)
# words that mark the rest as a project, so "start a focus timer" stays chat
PROJECT_WORDS = re.compile(
    r"\b(project|repo|codebase|app|website|site|bot|backend|frontend|dashboard|"
    r"game|assignment|lab|portal|api|server|landing|extension|agent|automation|"
    r"hackathon|competition|portfolio|tool|script|platform)\b", re.I)

PICK_PROMPT = (
    "You match a request to the user's coding projects. Below are candidate "
    "projects, one per line: id | name | folder | summary | tech. Choose the "
    "3 that best fit what the request means, best first, judging by meaning, "
    "not just shared words (e.g. 'carpool whatsapp bot' fits a project about "
    "ride sharing over WhatsApp). Leave out projects that clearly don't fit. "
    "Reply with JSON only: {\"top\": [<ids, best first, at most 3>]}."
    "\n\nCandidates:\n{catalog}")


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("_", " ").replace("-", " "))


def compact(text: str) -> str:
    return "".join(words(text))


# ---------- index ----------
def is_project(path: Path, names: set[str]) -> bool:
    if names & MARKERS or any(Path(n).suffix in MARKER_EXTS for n in names):
        return True
    return any(n.lower() in SUBAPPS and (path / n).is_dir() for n in names)


# template READMEs and agent-instruction preambles say nothing about the project
BOILERPLATE = re.compile(
    r"^(react \+ (vite|typescript)|this template|getting started|run and deploy|"
    r"read (this|docs)|@|both claude|this file|follow the user|currently, two "
    r"official plugins|this is a next\.?js project|welcome to)", re.I)
TEMPLATE_README = re.compile(
    r"template\s+provides\s+a\s+minimal\s+setup|bootstrapped\s+with\W+create-|"
    r"NextJS starter in Firebase Studio|contains everything you need to run\s+\w+",
    re.I)


def useful(text: str) -> str:
    """The text, or "" when it is starter boilerplate."""
    return "" if TEMPLATE_README.search(text) or BOILERPLATE.match(text) else text


def readme_summary(path: Path) -> str:
    for name in ("README.md", "readme.md", "README.MD", "Readme.md", "README.txt",
                 "AGENTS.md", "CLAUDE.md", "docs/PROJECT.md", "docs/README.md",
                 "QUICKSTART.md", "IMPLEMENTATION_SUMMARY.md", "Plan.md"):
        f = path / name
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            continue
        if TEMPLATE_README.search(text[:1500]):
            continue  # untouched starter README
        title, body = "", ""
        for line in text.splitlines():
            line = line.strip()
            if (not line or line.startswith(("![", "[![", "<", "```", "|", "---", "==="))
                    or BOILERPLATE.match(line)):
                continue
            if line.startswith("#"):
                heading = line.lstrip("# ").strip()
                if not BOILERPLATE.match(heading) and not re.search(
                        r"instructions|^purpose|^overview|^about", heading, re.I):
                    title = title or heading
                if body:
                    break
                continue
            body += " " + re.sub(r"[*_`>\[\]]|\(http\S*\)", "", line)
            if len(body) > 200:
                break
        out = (title + ": " if title else "") + body.strip()
        if out:
            return out[:220]
    return ""


def read_json(f: Path) -> dict:
    try:
        return json.loads(f.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def requirements(path: Path) -> list[str]:
    names: list[str] = []
    f = path / "requirements.txt"
    if f.is_file():
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line)
                if m and not line.strip().startswith("#"):
                    names.append(m.group(1).lower())
        except OSError:
            pass
    f = path / "pyproject.toml"
    if f.is_file():
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            block = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.S)
            if block:
                names += [m.lower() for m in re.findall(r"\"([A-Za-z0-9_.\-]+)", block.group(1))]
        except OSError:
            pass
    return names


def find_venv(*dirs: Path) -> Path | None:
    for d in dirs:
        for name in (".venv", "venv", "env"):
            act = d / name / "Scripts" / "Activate.ps1"
            if act.is_file():
                return act
    return None


def app_module(path: Path, marker: str) -> str | None:
    """'main:app' style module for the first file that creates `marker`."""
    for rel in ("main.py", "app.py", "server.py", "api.py", "app/main.py",
                "src/main.py", "app/app.py"):
        f = path / rel
        try:
            if f.is_file() and marker in f.read_text(encoding="utf-8", errors="replace"):
                return rel[:-3].replace("/", ".") + ":app"
        except OSError:
            continue
    return None


def dev_command(path: Path, root: Path) -> tuple[str, list[str]] | None:
    """(command, tech words) to start this folder's dev server, if it has one."""
    pkg = read_json(path / "package.json") if (path / "package.json").is_file() else None
    if pkg is not None:
        scripts = pkg.get("scripts") or {}
        deps = list((pkg.get("dependencies") or {})) + list((pkg.get("devDependencies") or {}))
        tech = [d for d in deps if not d.startswith("@types/")][:14]
        runner = ("pnpm" if (path / "pnpm-lock.yaml").exists()
                  else "yarn" if (path / "yarn.lock").exists() else "npm")
        script = next((s for s in ("dev", "start", "serve", "develop") if s in scripts), None)
        if not script:
            return None
        cmd = f"{runner} run {script}" if runner == "npm" and script != "start" else (
            f"{runner} start" if script == "start" else f"{runner} {script}")
        if not (path / "node_modules").is_dir():
            cmd = f"{runner} install && {cmd}"
        return cmd, tech
    reqs = requirements(path)
    if not reqs and not any((path / f).is_file() for f in ("manage.py", "main.py", "app.py")):
        return None
    venv = find_venv(path, root)
    pre = f". '{venv}' && " if venv else ""
    if (path / "manage.py").is_file():
        return pre + "python manage.py runserver", reqs[:14]
    if "streamlit" in reqs:
        for f in ("app.py", "main.py", "streamlit_app.py"):
            if (path / f).is_file():
                return pre + f"streamlit run {f}", reqs[:14]
    if {"fastapi", "uvicorn"} & set(reqs):
        mod = app_module(path, "FastAPI(")
        if mod:
            return pre + f"uvicorn {mod} --reload", reqs[:14]
    if "flask" in reqs:
        for f in ("app.py", "main.py", "run.py", "server.py"):
            if (path / f).is_file():
                return pre + f"python {f}", reqs[:14]
    return None


def describe(path: Path, root: Path) -> dict:
    names = {p.name for p in path.iterdir()}
    pkg = read_json(path / "package.json") if "package.json" in names else {}
    summary = readme_summary(path) or useful(str(pkg.get("description") or ""))
    if not summary and "index.html" in names:
        try:
            m = re.search(r"<title>(.*?)</title>", (path / "index.html").read_text(
                encoding="utf-8", errors="replace")[:4000], re.S | re.I)
            summary = m.group(1).strip() if m else ""
        except OSError:
            pass
    servers, tech = [], set(requirements(path))
    for sub in [path] + sorted(path / n for n in names
                               if n.lower() in SUBAPPS and (path / n).is_dir()):
        found = dev_command(sub, path)
        if found:
            cmd, sub_tech = found
            servers.append({"dir": str(sub), "cmd": cmd,
                            "label": sub.name if sub != path else "dev"})
            tech.update(sub_tech)
        if sub != path and not summary:
            summary = readme_summary(sub) or useful(str(
                read_json(sub / "package.json").get("description") or ""))
    if not summary:  # fall back to what the folder holds
        summary = "files: " + ", ".join(sorted(n for n in names if not n.startswith("."))[:10])
    try:
        mtime = max((p.stat().st_mtime for p in path.iterdir()), default=path.stat().st_mtime)
    except OSError:
        mtime = 0.0
    return {"name": path.name, "path": str(path),
            "folder": str(path.parent.relative_to(root)) if path.parent != root else "",
            "summary": re.sub(r"\s+", " ", summary)[:220],
            "tech": sorted(tech)[:14], "servers": servers[:3], "mtime": mtime}


def scan(roots: list[Path] = ROOTS) -> list[dict]:
    projects: list[dict] = []

    def walk(path: Path, root: Path, depth: int) -> None:
        try:
            names = {p.name for p in path.iterdir()}
        except OSError:
            return
        category = re.match(r"\d\d ", path.name)  # "02 Startup Projects" etc.
        if depth and not category and is_project(path, names):
            projects.append(describe(path, root))
            return
        if depth >= MAX_DEPTH:
            return
        for n in sorted(names):
            sub = path / n
            if n in SKIP or n.startswith((".", "~$")) or not sub.is_dir():
                continue
            walk(sub, root, depth + 1)

    for root in roots:
        if root.is_dir():
            walk(root, root, 0)
    return projects


def load_index(force: bool = False) -> list[dict]:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        if not force and time.time() - data.get("built", 0) < INDEX_MAX_AGE:
            return data["projects"]
    except (OSError, ValueError, KeyError):
        pass
    projects = scan()
    try:
        INDEX_FILE.write_text(json.dumps({"built": time.time(), "projects": projects}),
                              encoding="utf-8")
    except OSError:
        pass
    return projects


# ---------- matching ----------
def launch_query(prompt: str) -> str | None:
    """The project description if this prompt asks to open a project.

    "open ..." style requests and short "<name> project" lines count;
    obvious non-projects (timers, websites, apps) stay normal chat.
    """
    text = prompt.strip().rstrip(".!?")
    m = LAUNCH_RE.match(text) or BARE_RE.match(text)
    if not m:
        return None
    what = m.group("what").strip()
    return None if NOT_PROJECT.search(what) else what


def score(query: str, project: dict, recent: dict[str, float]) -> float:
    q = [w for w in words(query) if w not in STOPWORDS]
    if not q:
        return 0.0
    name_words = words(project["name"])
    name_c, q_c = compact(project["name"]), "".join(q)
    s = 0.0
    if q_c == name_c:
        s += 20
    elif len(q_c) >= 4 and (q_c in name_c or name_c in q_c):
        s += 12
    other = set(words(project["summary"] + " " + project["folder"] + " "
                      + " ".join(project["tech"])))
    for w in q:
        if w in name_words:
            s += 5
        elif any(difflib.SequenceMatcher(None, w, n).ratio() >= 0.8 for n in name_words):
            s += 3
        if w in other:
            s += 1.5
    if s and project["path"] in recent:
        s += 1  # tie-break toward projects you open a lot
    return s


def rank(query: str, projects: list[dict], recent: dict[str, float]) -> list[tuple[float, dict]]:
    scored = [(score(query, p, recent), p) for p in projects]
    return sorted((x for x in scored if x[0] > 0), key=lambda x: -x[0])


def clear_winner(query: str, ranked: list[tuple[float, dict]]) -> dict | None:
    """The project when the request is basically its name, so no model call.

    Any extra describing word ("whatsapp *carpool* bot") means the request
    is about meaning, not the name, and Haiku decides instead.
    """
    if not ranked:
        return None
    top, project = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0
    q = [w for w in words(query) if w not in STOPWORDS]
    name_words, name_c = words(project["name"]), compact(project["name"])
    covered = all(w in name_words or w in name_c or any(
        difflib.SequenceMatcher(None, w, n).ratio() >= 0.8 for n in name_words)
        for w in q)
    return project if covered and top >= 12 and top >= 1.3 * second else None


def catalog_line(i: int, p: dict) -> str:
    summary = p["summary"][:90]
    tech = ",".join(p["tech"][:5])
    return f"{i} | {p['name']} | {p['folder']} | {summary} | {tech}"


class Searcher:
    """Similarity search over the project index with a tiny static model.

    model2vec embeddings are a token lookup plus an average, so encoding the
    whole index takes ~20 ms and a query well under 1 ms. The model loads
    on first use and the matrix is rebuilt only when the index changes.
    """

    def __init__(self) -> None:
        self.model = None
        self.matrix = None
        self.key: tuple = ()

    def ready(self, projects: list[dict]) -> bool:
        try:
            import numpy as np
            from model2vec import StaticModel
        except ImportError:
            return False
        if self.model is None:
            try:
                if EMBED_MODEL.is_dir():
                    self.model = StaticModel.from_pretrained(str(EMBED_MODEL))
                else:
                    self.model = StaticModel.from_pretrained(EMBED_MODEL_ID)
                    self.model.save_pretrained(str(EMBED_MODEL))
            except Exception:  # missing or broken model: words only
                return False
        key = tuple(p["path"] for p in projects)
        if key != self.key:
            docs = [f"{p['name']}. {p['summary']} {p['folder']} "
                    f"{' '.join(p['tech'][:8])}" for p in projects]
            m = self.model.encode(docs)
            self.matrix = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
            self.key = key
        return True

    def shortlist(self, query: str, projects: list[dict], recent: dict[str, float],
                  k: int = SHORTLIST) -> list[dict]:
        """The k projects closest to the query (embeddings + word matches)."""
        lexical = [score(query, p, recent) for p in projects]
        if self.ready(projects):
            import numpy as np
            v = self.model.encode([query])[0]
            v = v / (np.linalg.norm(v) + 1e-9)
            total = self.matrix @ v + LEXICAL_WEIGHT * np.array(lexical)
            order = list(np.argsort(-total)[:k])
        else:
            order = sorted(range(len(projects)), key=lambda i: -lexical[i])[:k]
        return [projects[i] for i in order]


def pick_top3(claude: str, query: str, candidates: list[dict],
              ask_fn: "Callable[[str, str], str | None] | None" = None) -> list[dict] | None:
    """The model's best 3 of the shortlist, best first. None if the call failed.

    Uses Claude Code (Haiku) unless `ask_fn(system, prompt)` is given, which
    runs the same prompt on another provider.
    """
    catalog = "\n".join(catalog_line(i, p) for i, p in enumerate(candidates))
    system = PICK_PROMPT.replace("{catalog}", catalog)
    cmd = [claude, "-p", "--model", "haiku", "--effort", "low",
           "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--setting-sources", "", "--no-session-persistence",
           "--system-prompt", system]
    try:
        if ask_fn:
            reply = ask_fn(system, f"Request: {query}") or ""
        else:
            out = subprocess.run(cmd, input=f"Request: {query}", capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 timeout=90, creationflags=CREATE_NO_WINDOW,
                                 cwd=Path.home(),
                                 env={**os.environ, "CLAUDE_PET_CHILD": "1"})
            reply = json.loads(out.stdout).get("result", "")
        found = re.search(r"\{.*\}", reply, re.S)
        ids = json.loads(found.group(0)).get("top", []) if found else None
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return None
    if not isinstance(ids, list):
        return None
    picked: list[dict] = []
    for i in ids:
        if isinstance(i, int) and 0 <= i < len(candidates) and candidates[i] not in picked:
            picked.append(candidates[i])
    return picked[:3]


# ---------- launching ----------
def pick_editor(preferred: str | None = None) -> str | None:
    """VS Code CLI to use: the saved choice, else the build that's running."""
    builds = {"insiders": shutil.which("code-insiders"), "stable": shutil.which("code")}
    if preferred in builds and builds[preferred]:
        return builds[preferred]
    try:
        running = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                                 text=True, timeout=10,
                                 creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        running = ""
    if "Code - Insiders.exe" in running and builds["insiders"]:
        return builds["insiders"]
    if '"Code.exe"' in running and builds["stable"]:
        return builds["stable"]
    return builds["insiders"] or builds["stable"]


def ext_manifest() -> dict:
    return json.loads((EXT_DIR / "package.json").read_text(encoding="utf-8"))


def ext_installed(editor: str) -> bool:
    """Whether this VS Code build has the current companion extension."""
    m = ext_manifest()
    home = ".vscode-insiders" if "insiders" in editor.lower() else ".vscode"
    folder = Path.home() / home / "extensions"
    return any(folder.glob(f"{m['publisher']}.{m['name']}-{m['version']}*"))


def build_vsix(out: Path) -> Path:
    """Pack vscode-ext/ into a .vsix (a zip with a small manifest)."""
    m = ext_manifest()
    ident = (f'<Identity Language="en-US" Id="{m["name"]}" Version="{m["version"]}" '
             f'Publisher="{m["publisher"]}"/>')
    manifest = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">'
        f'<Metadata>{ident}<DisplayName>{m["displayName"]}</DisplayName>'
        f'<Description xml:space="preserve">{m["description"]}</Description>'
        '<Properties><Property Id="Microsoft.VisualStudio.Code.Engine" '
        f'Value="{m["engines"]["vscode"]}"/></Properties></Metadata>'
        '<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>'
        '<Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" '
        'Path="extension/package.json" Addressable="true"/></Assets></PackageManifest>')
    types = ('<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension=".json" ContentType="application/json"/>'
             '<Default Extension=".js" ContentType="application/javascript"/>'
             '<Default Extension=".vsixmanifest" ContentType="text/xml"/></Types>')
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("extension.vsixmanifest", manifest)
        for f in ("package.json", "extension.js"):
            z.write(EXT_DIR / f, f"extension/{f}")
    return out


def ensure_extension(editor: str | None) -> bool:
    """Install the companion extension into this VS Code build if missing."""
    if not editor:
        return False
    if ext_installed(editor):
        return True
    vsix = build_vsix(Path(os.environ.get("TEMP", Path.home())) / "claude-pet-launcher.vsix")
    try:
        subprocess.run([editor, "--install-extension", str(vsix), "--force"],
                       capture_output=True, timeout=120, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return ext_installed(editor)


def terminal_plan(project: dict, servers: bool) -> list[dict]:
    """Terminals to open: dev servers first, Claude Code last (focused)."""
    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    venv = find_venv(Path(project["path"]))
    plan = []
    if servers:
        plan += [{"name": f"{s['label']} server", "cwd": s["dir"], "shell": shell,
                  "commands": [s["cmd"]]} for s in project["servers"]]
    plan.append({"name": "Claude", "cwd": project["path"], "shell": shell, "claude": True,
                 "commands": ([f". '{venv}'"] if venv else []) + [CLAUDE_COMMAND]})
    return plan


def wt_escape(text: str) -> str:
    return text.replace(";", "\\;")  # wt splits its own commands on ';'


def launch(project: dict, editor: str | None, servers: bool = True) -> list[str]:
    """Open the project. Returns what was opened, for the pet to report."""
    path = project["path"]
    opened = []
    server_notes = [f"`{s['cmd'].split(' && ')[-1]}`"
                    + (f" ({s['label']})" if s["label"] != "dev" else "")
                    for s in project["servers"]] if servers else []
    if editor and ext_installed(editor):
        # VS Code's own terminal: the window with this folder picks this up.
        VSCODE_REQUEST.write_text(json.dumps({
            "folder": path, "ts": time.time(),
            "terminals": terminal_plan(project, servers)}), encoding="utf-8")
        subprocess.Popen([editor, path], creationflags=CREATE_NO_WINDOW)
        return ["VS Code", "Claude in its terminal"] + server_notes
    if editor:
        subprocess.Popen([editor, path], creationflags=CREATE_NO_WINDOW)
        opened.append("VS Code")
    shell = shutil.which("pwsh") or "powershell"
    venv = find_venv(Path(path))
    first = (f". '{venv}'; " if venv else "") + CLAUDE_COMMAND
    tabs = [(project["name"][:24], path, first)]
    if servers:
        tabs += [(f"{s['label']} server", s["dir"], s["cmd"]) for s in project["servers"]]
    wt = shutil.which("wt")
    if wt:
        args = [wt, "-w", "new"]
        for i, (title, cwd, cmd) in enumerate(tabs):
            if i:
                args.append(";")
                args.append("new-tab")
            args += ["--title", wt_escape(title), "-d", cwd, shell, "-NoExit"]
            if cmd:
                args += ["-Command", wt_escape(cmd)]
        if len(tabs) > 1:
            args += [";", "focus-tab", "-t", "0"]
        subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
    else:
        for _title, cwd, cmd in tabs:
            subprocess.Popen([shell, "-NoExit"] + (["-Command", cmd] if cmd else []),
                             cwd=cwd, creationflags=CREATE_NEW_CONSOLE)
    opened.append("terminal")
    return opened + server_notes
