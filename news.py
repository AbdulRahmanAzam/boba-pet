"""AI and tech news for the morning briefing, from real feeds.

The briefing used to ask the model to web-search and write the links
itself. Web search often returned SEO spam pages, and the model filled in
links and dates it had never opened. Now the pet reads RSS/Atom feeds from
known publishers itself (no tokens), numbers the items, and the model only
picks the important ones and explains them by number. Headline links,
publisher names and dates are put back in by this module from the feed
data, so every source is a real article from the stated outlet.
"""

import concurrent.futures as cf
import html
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

ATOM = "{http://www.w3.org/2005/Atom}"
GNEWS = "https://news.google.com/rss/search?q={q}+when:7d&hl=en-US&gl=US&ceid=US:en"
# (publisher shown when the feed doesn't name one, feed URL)
FEEDS = (
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Google", "https://blog.google/technology/ai/rss/"),
    ("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    ("Hacker News", "https://hnrss.org/frontpage?points=300"),
    ("Claude Code", "https://github.com/anthropics/claude-code/releases.atom"),
    ("Codex CLI", "https://github.com/openai/codex/releases.atom"),
    ("Google News", GNEWS.format(q="Anthropic+OR+%22Claude+Code%22")),
    ("Google News", GNEWS.format(q="OpenAI+OR+ChatGPT+OR+Codex")),
    ("Google News", GNEWS.format(q="Gemini+OR+DeepMind+OR+%22Google+AI%22")),
    ("Google News", GNEWS.format(q="%22AI+model%22+launch+OR+release")),
)
PER_FEED = 12
MAX_ITEMS = 80
WINDOW_DAYS = 7
SNIPPET_CHARS = 160
PRERELEASE = re.compile(r"alpha|beta|rc\d|-pre|nightly", re.I)

PICK_PROMPT = """Today is {today}. Below are numbered news items from the past week, read from publisher feeds. Pick the {count} most important for someone who builds with AI: new AI models, AI developer tools (Claude Code, Codex, Cursor, Copilot), major AI company news, notable tech launches. Prefer items published since {since}. Skip minor product tie-ins, opinion pieces, duplicates and anything not actually news.

Reply with exactly {count} lines, most important first, each:
- [N] **Short headline** - why it matters, in one sentence
N is the item number. Use only what the item says; do not add facts, dates or links. No intro or outro.

{items}"""


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _clean(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def _date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _parse(feed_name: str, raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):  # RSS
        title = _clean(_text(it.find("title")))
        source = it.find("source")
        publisher = _text(source) or feed_name
        if source is not None and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3]  # Google News appends it
        out.append({"title": title, "url": _text(it.find("link")),
                    "date": _date(_text(it.find("pubDate"))), "publisher": publisher,
                    "snippet": _clean(_text(it.find("description")))})
    for it in root.iter(ATOM + "entry"):  # Atom
        link = it.find(ATOM + "link[@rel='alternate']")
        if link is None:
            link = it.find(ATOM + "link")
        title = _clean(_text(it.find(ATOM + "title")))
        if feed_name in ("Claude Code", "Codex CLI"):
            if PRERELEASE.search(title):
                continue
            title = f"{feed_name} {title} released"
        out.append({"title": title,
                    "url": link.get("href", "") if link is not None else "",
                    "date": _date(_text(it.find(ATOM + "published"))
                                  or _text(it.find(ATOM + "updated"))),
                    "publisher": feed_name,
                    "snippet": _clean(_text(it.find(ATOM + "summary"))
                                      or _text(it.find(ATOM + "content")))})
    return out


def _fetch(feed: tuple[str, str]) -> list[dict]:
    name, url = feed
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 claude-pet"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            items = _parse(name, resp.read())
    except (OSError, ET.ParseError, ValueError):
        return []
    items = [i for i in items if i["title"] and i["url"].startswith("http") and i["date"]]
    items.sort(key=lambda i: i["date"], reverse=True)
    if name in ("Claude Code", "Codex CLI"):
        items = items[:1]  # just the newest stable release
    return items[:PER_FEED]


def _key(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.lower())[:8])


def collect(now: datetime | None = None) -> list[dict]:
    """News items from the last WINDOW_DAYS, newest first, deduplicated."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    with cf.ThreadPoolExecutor(8) as pool:
        batches = list(pool.map(_fetch, FEEDS))
    seen, items = set(), []
    for item in sorted((i for b in batches for i in b), key=lambda i: i["date"],
                       reverse=True):
        key = _key(item["title"])
        if item["date"] < cutoff or item["date"] > now + timedelta(hours=12) or key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items[:MAX_ITEMS]


def day_label(when: datetime) -> str:
    local = when.astimezone()
    return f"{local:%b} {local.day}"


def pick_prompt(items: list[dict], since: str, count: int = 5) -> str:
    lines = []
    for n, it in enumerate(items, 1):
        snippet = it["snippet"][:SNIPPET_CHARS]
        lines.append(f"[{n}] {day_label(it['date'])} | {it['publisher']} | {it['title']}"
                     + (f" | {snippet}" if snippet and snippet != it["title"] else ""))
    return PICK_PROMPT.format(today=time.strftime("%Y-%m-%d"), since=since,
                              count=count, items="\n".join(lines))


def source_name(item: dict) -> str:
    """Outlet name for the link ("ABC News - Breaking News…" -> "ABC News").
    Hacker News items link to the story itself, so they show its site."""
    if item["publisher"] not in ("Google News", "Hacker News", ""):
        return item["publisher"].split(" - ")[0].split(" | ")[0].strip()
    return urlparse(item["url"]).netloc.removeprefix("www.")


def render(reply: str, items: list[dict]) -> str:
    """Model lines "- [N] **Headline** - why" into bullets with real links.

    Lines naming an unknown item are dropped, so nothing unsourced shows.
    """
    out, used = [], set()
    for line in reply.splitlines():
        m = re.match(r"\s*[-*•]?\s*\[(\d+)\]\s*(.+)", line)
        if not m:
            continue
        n = int(m.group(1))
        if not 1 <= n <= len(items) or n in used:
            continue
        used.add(n)
        it = items[n - 1]
        body = m.group(2).strip()
        head, _, why = body.partition(" - ")
        out.append(f"- {head.strip()} ({day_label(it['date'])}) - {why.strip() or it['title']}"
                   f" ([{source_name(it)}]({it['url']}))")
    return "\n".join(out)


def fallback(items: list[dict], count: int = 5) -> str:
    """Headlines straight from the feeds, when the model call fails."""
    return "\n".join(f"- **{it['title']}** ({day_label(it['date'])}) "
                     f"([{source_name(it)}]({it['url']}))" for it in items[:count])
