"""Hackathon mode: one workspace per hackathon, plus a prompt coach.

hackathons.py keeps the events themselves (shared with the hack-clock mod).
This module keeps what the pet adds on top, per event, in
~/.claude-pet-hackmode.json:

- the project folder you build it in,
- a submission checklist (made from the event's details, editable),
- notes, focus time spent, coaching stats,
- which deadline nudges were already shown,
- which hackathon is "active" (hackathon mode on).

The prompt coach: while hackathon mode is on and coaching is enabled, every
prompt you type into Claude Code inside the linked project folder is read
from the session transcript (cc_watch) and sent, with the hackathon's facts
and time left, to the cheapest model. It answers with a sharper prompt and
a tip or two. This is the one place where your Claude Code prompts leave
the machine, so it is a switch in the hackathon's workspace.
"""

import json
import os
import re
import time
import uuid
from pathlib import Path

import hackathons

DATA_FILE = Path.home() / ".claude-pet-hackmode.json"
COACH_MIN_CHARS = 25  # shorter prompts ("yes", "go on") are not coached
COACH_GAP_SECONDS = 120  # at most one coaching bubble this often
SPRINT_MINUTES = 25
# (hours before the submission deadline, nudge)
MILESTONES = (
    (48, "2 days left. Lock the scope: pick the one feature judges must see working."),
    (24, "24 hours left. Get the core flow working end to end, then plan the demo."),
    (12, "12 hours left. Start the README and record a rough demo now, not later."),
    (6, "6 hours left. Feature freeze. Fix bugs, polish the demo, write the pitch."),
    (2, "2 hours left. Submit a working version now; you can update it later."),
    (0.5, "30 minutes left! Submit now if you haven't."),
)

COACH_PROMPT = """You coach someone at a hackathon who is prompting Claude Code (an AI coding agent working in their repo). Make their prompt get better results, faster.

Hackathon: {name}
Theme / rules: {theme}
Time left to submit: {left}
Project: {project}
Still to do: {todo}

Their prompt:
<prompt>
{prompt}
</prompt>

Reply in exactly this format:
SCORE: <1-10, how well the prompt will work as written>
BETTER:
<the improved prompt, ready to paste. Keep their intent and wording where it is fine. Add what is missing: the exact goal, files or components, constraints, how to verify (run, test), and what to leave alone. Keep it short; no filler.>
TIPS:
- <one hackathon-aware tip, e.g. scope, demo, time, judging>
- <optional second tip>
If the prompt is already good (8+), say so in TIPS and keep BETTER close to the original."""


# ---------- storage ----------
def load() -> dict:
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DATA_FILE)
    except OSError:
        pass


def workspace(data: dict, event: dict) -> dict:
    """The event's workspace, created with a starter checklist on first use."""
    spaces = data.setdefault("events", {})
    ws = spaces.get(event["id"])
    if ws is None:
        ws = spaces[event["id"]] = {
            "project": "", "notes": "", "checklist": default_checklist(event),
            "focus_minutes": 0, "sprints": 0, "coached": 0, "nudged": [],
            "coach": True}
    return ws


def active_id(data: dict) -> str:
    return data.get("active") or ""


# ---------- checklist ----------
def _item(text: str) -> dict:
    return {"id": uuid.uuid4().hex[:8], "text": text, "done": False}


def default_checklist(event: dict) -> list[dict]:
    details = event.get("details") or {}
    items = []
    if not event.get("isRegistered"):
        items.append("Register")
    team = str(details.get("teamSize") or "")
    if team and not re.fullmatch(r"(?i)solo|1|individual", team.strip()):
        items.append(f"Team formed ({team})")
    if details.get("theme"):
        items.append(f"Idea fits the theme: {details['theme']}")
    else:
        items.append("Idea locked (one sentence pitch)")
    items += ["Repo created and pushed", "Core feature works end to end",
              "README: problem, how it works, setup steps",
              "Demo video (2-3 min)", "Pitch deck / slides",
              "Deployed or runnable demo link", "Submission form filled"]
    if details.get("community"):
        items.insert(1, "Joined the community / Discord")
    return [_item(t) for t in items]


def add_item(ws: dict, text: str) -> None:
    if text.strip():
        ws["checklist"].append(_item(text.strip()))


def toggle_item(ws: dict, item_id: str) -> None:
    for it in ws["checklist"]:
        if it["id"] == item_id:
            it["done"] = not it["done"]


def remove_item(ws: dict, item_id: str) -> None:
    ws["checklist"] = [it for it in ws["checklist"] if it["id"] != item_id]


def open_items(ws: dict) -> list[str]:
    return [it["text"] for it in ws["checklist"] if not it["done"]]


def readiness(ws: dict) -> tuple[int, int]:
    done = sum(1 for it in ws["checklist"] if it["done"])
    return done, len(ws["checklist"])


# ---------- time ----------
def submit_at(event: dict) -> float | None:
    return hackathons._instant(event.get("submissionDeadline"))


def time_left_text(event: dict, now: float) -> str:
    kind, at = hackathons.phase(event, now)
    if kind == "submit" and at:
        return hackathons.duration(at - now)
    return hackathons.countdown(event, kind, at, now)


def badge_text(event: dict, now: float) -> str:
    """Short text for the badge under the pet: "⚡ NaSCon · 1d 4h"."""
    kind, at = hackathons.phase(event, now)
    name = event.get("name", "Hackathon")
    name = name if len(name) <= 18 else name[:17] + "…"
    if kind in ("submit", "register", "starts") and at:
        verb = {"submit": "", "register": "register ", "starts": "starts "}[kind]
        return f"⚡ {name} · {verb}{hackathons.duration(at - now)}"
    return f"⚡ {name} · {hackathons.countdown(event, kind, at, now)}"


def due_milestone(event: dict, ws: dict, now: float) -> tuple[float, str] | None:
    """The newest deadline nudge that is due and not shown yet."""
    kind, at = hackathons.phase(event, now)
    if kind != "submit" or at is None:
        return None
    left_h = (at - now) / 3600
    due = [(h, text) for h, text in MILESTONES if left_h <= h and h not in ws["nudged"]]
    if not due:
        return None
    for h, _t in due:  # older ones were missed while away: count them as shown
        ws["nudged"].append(h)
    return min(due)


# ---------- project folder ----------
def guess_project(event: dict, index: list[dict]) -> list[dict]:
    """Projects whose names look like the event's, best first (no model call)."""
    import projects  # local import: projects pulls in more than this module needs
    name = event.get("name", "")
    query = re.sub(r"(?i)\b(hackathon|hack|20\d\d|'\d\d|cup|global|challenge)\b", " ", name)
    ranked = projects.rank(query.strip() or name, index, {})
    return [p for score, p in ranked if score > 0][:5]


# ---------- context for the pet's own chats ----------
def chat_context(event: dict, ws: dict, now: float) -> str:
    details = event.get("details") or {}
    todo = open_items(ws)[:6]
    bits = [f"The user is in hackathon mode for {event.get('name')}"
            f" ({time_left_text(event, now)} to submit)"]
    if details.get("theme"):
        bits.append(f"theme: {details['theme']}")
    if ws.get("project"):
        bits.append(f"project folder: {ws['project']}")
    if todo:
        bits.append("still to do: " + "; ".join(todo))
    return " " + ", ".join(bits) + ". Keep advice hackathon-practical: scope, demo, deadline."


# ---------- prompt coach ----------
def worth_coaching(prompt: str) -> bool:
    text = prompt.strip()
    return (len(text) >= COACH_MIN_CHARS and not text.startswith(("/", "!", "<"))
            and "[Request interrupted" not in text)


def coach_prompt(event: dict, ws: dict, prompt: str, now: float) -> str:
    details = event.get("details") or {}
    theme = " · ".join(str(v) for v in (details.get("theme"), details.get("notes")) if v)
    return COACH_PROMPT.format(
        name=event.get("name", "Hackathon"), theme=theme or "not given",
        left=time_left_text(event, now),
        project=ws.get("project") or "not linked",
        todo="; ".join(open_items(ws)[:6]) or "nothing listed",
        prompt=prompt.strip()[:4000])


def parse_coach(reply: str) -> dict | None:
    """{"score": int, "better": str, "tips": [str]} from the model's reply."""
    m = re.search(r"SCORE:\s*(\d+)", reply)
    better = re.search(r"BETTER:\s*\n?(.*?)(?:\n\s*TIPS:|\Z)", reply, re.S)
    tips = re.search(r"TIPS:\s*(.*)", reply, re.S)
    if not better or not better.group(1).strip():
        return None
    tip_lines = [t.strip(" -•*\t") for t in (tips.group(1).splitlines() if tips else [])]
    return {"score": max(1, min(10, int(m.group(1)))) if m else 0,
            "better": better.group(1).strip().strip("`").strip(),
            "tips": [t for t in tip_lines if t][:2]}


def in_folder(cwd: str, folder: str) -> bool:
    if not cwd or not folder:
        return False
    a = os.path.normcase(os.path.abspath(cwd))
    b = os.path.normcase(os.path.abspath(folder))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def now() -> float:
    return time.time()
