"""
intent_parser.py - rule-based intent understanding.

Turns a free-text request into a small, validated task description. The raw prompt is
only ever used for keyword matching and URL extraction; it is NEVER passed to a shell
or forwarded to the CLI. Only the extracted URL and a bounded integer are.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

MAX_PAGES_LIMIT = 10

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
BARE_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|in|edu|gov|co|dev|app|ai|info|me|uk|de|fr|us|ca|au|xyz|tech|site|online|blog|news)\b(?:/[^\s<>\"'`]*)?",
    re.I,
)
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10}
PAGES_RE = re.compile(r"\b(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")\s+(?:\w+\s+)?pages?\b", re.I)

CRAWL_WORDS = ("crawl", "scrape", "fetch", "visit", "browse", "explore", "scan", "gather",
               "collect", "read", "extract", "analy", "inspect", "look at", "check")
SUMMARY_WORDS = ("summar", "overview", "tl;dr", "tldr", "key points", "gist", "important information",
                 "what is", "what's on", "describe", "main points")
JSON_WORDS = ("json", "structured", "extract", "data", "fields")
SINGLE_PAGE_WORDS = ("single page", "only this page", "just this page", "home page only", "homepage only", "one page only")


@dataclass
class Intent:
    prompt: str
    url: str | None = None
    action: str = "crawl"
    want_summary: bool = True
    want_json: bool = True
    max_pages: int = 3
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("prompt", None)
        return d


def _find_url(text: str) -> str | None:
    m = URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;:!?)]}")
    m = BARE_RE.search(text)
    if m:
        return "https://" + m.group(0).rstrip(".,;:!?)]}")
    return None


def _find_max_pages(text: str, default: int, notes: list) -> int:
    lowered = text.lower()
    if any(w in lowered for w in SINGLE_PAGE_WORDS):
        notes.append("Single page requested.")
        return 1
    m = PAGES_RE.search(lowered)
    if not m:
        return default
    raw = m.group(1).lower()
    n = NUMBER_WORDS.get(raw) or int(raw)
    clamped = max(1, min(n, MAX_PAGES_LIMIT))
    if clamped != n:
        notes.append(f"Requested {n} pages; limited to {clamped} for safety.")
    return clamped


def parse_request(prompt: str, default_pages: int = 3) -> Intent:
    text = (prompt or "").strip()
    intent = Intent(prompt=text, max_pages=max(1, min(default_pages, MAX_PAGES_LIMIT)))
    if not text:
        intent.error = "Please describe a task, for example: *Crawl https://example.com and summarise it as JSON.*"
        return intent
    if len(text) > 2000:
        intent.error = "That request is too long (limit 2000 characters)."
        return intent

    intent.url = _find_url(text)
    if not intent.url:
        intent.error = ("I couldn't find a website URL in your request. This prototype crawls websites, "
                        "so please include one, e.g. `https://example.com`.")
        return intent

    lowered = text.lower()
    wants_summary = any(w in lowered for w in SUMMARY_WORDS)
    wants_json = any(w in lowered for w in JSON_WORDS)
    if not wants_summary and not wants_json:
        wants_summary = wants_json = True
        intent.notes.append("No output format specified; returning a summary and JSON.")
    intent.want_summary, intent.want_json = wants_summary, wants_json
    if not any(w in lowered for w in CRAWL_WORDS) and not (wants_summary or wants_json):
        intent.notes.append("Assuming you want the site crawled.")

    intent.max_pages = _find_max_pages(text, intent.max_pages, intent.notes)

    outputs = " + ".join(x for x, on in (("summary", intent.want_summary), ("JSON", intent.want_json)) if on)
    intent.steps = [
        f"Validate URL and limits (http/https only, public hosts only, same domain, max {intent.max_pages} pages)",
        "Launch `crawler_cli.py` as a separate process (no shell, fixed argument list)",
        "Capture stdout, stderr and the exit code; enforce timeout",
        "Validate the returned JSON against the expected structure",
        f"Build the chat response: {outputs}",
    ]
    return intent
