"""
summarizer.py - extractive summariser (no LLM, no API key).

Picks the most informative sentences from the text that was actually crawled, scoring
sentences by the frequency of their meaningful words. Because every output sentence is
copied from the crawled pages, the summary cannot hallucinate and website text cannot
instruct the summariser to do anything.
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

STOPWORDS = set("""a about above after again all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her
here hers him his how i if in into is it its just me more most my no nor not now of off on once only or other our
out over own same she should so some such than that the their them then there these they this those through to too
under until up very was we were what when where which while who whom why will with you your""".split())

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")


def _sentences(text: str):
    for s in SENTENCE_SPLIT.split(text.strip()):
        s = s.strip()
        words = s.split()
        if 4 <= len(words) <= 60 and s[-1] in ".!?\"'”’)":
            yield s


def summarize(pages: list, max_sentences: int = 4) -> dict:
    ok = [p for p in pages if p.get("status") == "ok"]
    if not ok:
        return {"headline": "", "points": []}

    candidates = []  # (sentence, bonus)
    for page in ok:
        if page.get("description"):
            for s in _sentences(page["description"]):
                candidates.append((s, 1.5))
        for i, para in enumerate(page.get("paragraphs", [])):
            for s in _sentences(para):
                candidates.append((s, 1.2 if i == 0 else 1.0))

    seen, unique = set(), []
    for s, bonus in candidates:
        key = s.lower()
        if key not in seen:
            seen.add(key)
            unique.append((s, bonus))

    freq = Counter()
    for s, _ in unique:
        freq.update(w.lower() for w in WORD_RE.findall(s) if w.lower() not in STOPWORDS)

    scored = []
    for idx, (s, bonus) in enumerate(unique):
        words = [w.lower() for w in WORD_RE.findall(s) if w.lower() not in STOPWORDS]
        if not words:
            continue
        score = bonus * sum(freq[w] for w in words) / (len(words) ** 0.6)
        scored.append((score, idx, s))
    top = sorted(scored, reverse=True)[:max_sentences]
    points = [s for _, _, s in sorted(top, key=lambda t: t[1])]

    host = urlparse(ok[0]["url"]).hostname or ""
    title = ok[0].get("title") or host
    headline = f"{title} — {len(ok)} page{'s' if len(ok) != 1 else ''} crawled from {host}"
    if not points and ok[0].get("headings"):
        points = [h["text"] for h in ok[0]["headings"][:max_sentences]]
    return {"headline": headline, "points": points}
