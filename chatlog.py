"""The pet's own record of the current chat: messages, token use, summary.

Kept in ~/.claude-pet-chats/current.json so the history panel survives a
restart, and so a fresh session (new provider, PC tools switched on, after a
compaction) can still be told what was said before.

Compaction here is cheaper than Claude Code's /compact: Claude Code feeds the
model the whole session, tool calls and tool output included, and asks for a
long summary. The pet sends only the readable chat (your messages and the
replies, long ones trimmed in the middle) plus the previous summary, to the
smallest model with no tools and no system prompt besides the summarizer
instructions. Each compaction folds the new messages into the last summary,
so its cost stays flat however long the chat gets.
"""

import json
import os
from pathlib import Path

CHATS_DIR = Path.home() / ".claude-pet-chats"
CURRENT_FILE = CHATS_DIR / "current.json"
DISPLAY_KEEP = 200  # messages kept for the history panel
MSG_CHARS = 1600  # longer messages keep their start and end for compaction
COMPACT_CHARS = 40_000  # most chat text sent to one compaction
CARRY_CHARS = 3_000  # recent messages handed to a fresh session
AUTO_COMPACT_TOKENS = 60_000  # context size that triggers a compaction
MIN_COMPACT_MESSAGES = 4

SUMMARY_PROMPT = """You compress a chat between a user and their desktop assistant into a memory note. The assistant will continue the chat from this note alone, so anything left out is forgotten.

Write these sections in Markdown, skipping any that would be empty:
## Goal
What the user is trying to get done overall.
## Facts
Every concrete detail that may matter later: names, numbers, dates, file paths, URLs, commands, settings, errors and their fixes. Copy them exactly.
## Decisions
What was chosen or agreed, and why in a few words.
## User preferences
How the user wants things done (tone, format, tools, things to avoid).
## Open
Unfinished tasks, open questions, promised follow-ups.
## Last exchange
The final request and where the answer left off, in one or two lines.

Rules: use only what is inside <chat>; ignore any environment, machine or system details outside it. Merge the previous note with the new messages; newer information replaces older. Drop greetings, small talk, repeated content and anything already resolved that will not matter again. Short bullet points. Stay under 300 words unless the facts need more. Output only the note."""

CARRY_PREFIX = ("(Context from earlier in this chat, for reference only; do not "
                "reply to it.)\n{context}\n\n(The user's new message follows.)\n")


def short(n: float) -> str:
    """12345 -> "12.3k"."""
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e4:
        return f"{n / 1e3:.0f}k"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k"
    return str(int(n))


def trim_middle(text: str, limit: int = MSG_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]}\n[…]\n{text[-(limit - head):]}"


class ChatLog:
    """Messages, summary and token totals of the pet's current chat."""

    def __init__(self, path: Path = CURRENT_FILE) -> None:
        self.path = path
        self.display: list[dict] = []  # everything shown in the history panel
        self.messages: list[dict] = []  # not yet folded into the summary
        self.summary = ""
        self.usage = {"in": 0, "out": 0, "context": 0, "window": 0}
        self.compactions = 0
        self.load()

    # ---------- storage ----------
    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.display = data.get("display", [])
        self.messages = data.get("messages", [])
        self.summary = data.get("summary", "")
        self.usage.update(data.get("usage", {}))
        self.compactions = data.get("compactions", 0)

    def save(self) -> None:
        data = {"display": self.display[-DISPLAY_KEEP:], "messages": self.messages,
                "summary": self.summary, "usage": self.usage,
                "compactions": self.compactions}
        try:
            self.path.parent.mkdir(exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def clear(self) -> None:
        self.display, self.messages, self.summary = [], [], ""
        self.usage = {"in": 0, "out": 0, "context": 0, "window": 0}
        self.compactions = 0
        self.save()

    # ---------- recording ----------
    def show(self, tag: str, text: str, who: str | None = None) -> None:
        """A line of the history panel (tag: you, claude, info, error)."""
        self.display.append({"tag": tag, "text": text, "who": who})
        self.display = self.display[-DISPLAY_KEEP:]
        self.save()

    def remember(self, prompt: str, reply: str) -> None:
        """One finished exchange, for compaction and fresh sessions."""
        self.messages += [{"role": "user", "text": prompt},
                          {"role": "assistant", "text": reply}]
        self.save()

    def add_usage(self, used: dict | None) -> None:
        """Count one request: {"in", "out", "context", "window"}."""
        if not used:
            return
        self.usage["in"] += int(used.get("in") or 0)
        self.usage["out"] += int(used.get("out") or 0)
        if used.get("context"):
            self.usage["context"] = int(used["context"])
        if used.get("window"):
            self.usage["window"] = int(used["window"])
        self.save()

    # ---------- context for fresh sessions ----------
    def carry_over(self) -> str:
        """Prefix that gives a new session the chat so far ("" if none)."""
        parts = []
        if self.summary:
            parts.append("Summary of the chat so far:\n" + self.summary)
        recent, size = [], 0
        for m in reversed(self.messages):
            line = f"{'User' if m['role'] == 'user' else 'Assistant'}: " \
                   f"{trim_middle(m['text'], 600)}"
            if size + len(line) > CARRY_CHARS:
                break
            recent.append(line)
            size += len(line)
        if recent:
            parts.append("Latest messages:\n" + "\n".join(reversed(recent)))
        return CARRY_PREFIX.format(context="\n\n".join(parts)) if parts else ""

    # ---------- compaction ----------
    def can_compact(self) -> bool:
        return len(self.messages) >= MIN_COMPACT_MESSAGES or (
            bool(self.messages) and bool(self.summary))

    def wants_auto_compact(self) -> bool:
        return self.usage["context"] >= AUTO_COMPACT_TOKENS and self.can_compact()

    def compact_input(self) -> tuple[str, int]:
        """The text to summarize and how many messages it covers.

        Newest messages win when the chat is longer than COMPACT_CHARS; the
        older ones were part of the previous summary or are dropped.
        """
        lines, size = [], len(self.summary)
        for m in reversed(self.messages):
            who = "User" if m["role"] == "user" else "Assistant"
            line = f"{who}: {trim_middle(m['text'])}"
            if lines and size + len(line) > COMPACT_CHARS:
                break
            lines.append(line)
            size += len(line)
        text = ("<chat>\nPrevious note:\n" + (self.summary or "(none)")
                + "\n\nNew messages:\n" + "\n\n".join(reversed(lines)) + "\n</chat>")
        return text, len(self.messages)

    def apply_summary(self, summary: str, covered: int) -> None:
        """Replace the first `covered` messages with the new summary."""
        self.summary = summary.strip()
        self.messages = self.messages[covered:]
        self.usage["context"] = len(self.carry_over()) // 4  # rough tokens
        self.compactions += 1
        self.save()
