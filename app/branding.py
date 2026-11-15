"""Detect and remove a channel's own "signature" from captions.

Movie channels often stamp every upload with their name, e.g.
"קינג סרט ספיידרמן 3", "מדיה VOD ספיידרמן 3 הועלה ע"י יחיאל" or a line like "🎬 חנואל סרטים 🎬".
Without removing it, the same movie from different channels looks like different movies.

Detection is statistical and per channel: a line, or the first/last words of the name, that
repeat across many *different* movies of the same channel are the channel's signature.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .parser import EPISODE_PATTERNS, clean_name, normalize

MIN_MOVIES = 20        # need this many movie captions in a channel before guessing
MIN_COUNT = 10         # a signature must appear at least this often...
MIN_SHARE = 0.02       # ...and in at least 2% of the channel's movies
MIN_VARIETY = 10       # ...followed by at least this many different names (so "ספיידרמן" is not a signature)


def _has_episode(text: str) -> bool:
    flat = re.sub(r"[._]+", " ", text)
    return any(p.search(flat) for p in EPISODE_PATTERNS)


def _norm_line(line: str) -> str:
    return normalize(clean_name(line))


def detect(captions: list[str | None]) -> dict:
    movies = [c for c in captions if c and c.strip() and not _has_episode(c)]
    n = len(movies)
    if n < MIN_MOVIES:
        return {}
    threshold = max(MIN_COUNT, n * MIN_SHARE)

    # 1. whole lines that repeat (e.g. "🎬 קינג סרט 🎬", "הצטרפו לערוץ!")
    line_count: Counter = Counter()
    for c in movies:
        line_count.update({_norm_line(l) for l in c.splitlines()} - {""})
    lines = {l for l, k in line_count.items() if k >= threshold}

    # 2. first / last words of the name line
    pre_count: Counter = Counter()
    suf_count: Counter = Counter()
    pre_next: dict = defaultdict(set)
    suf_prev: dict = defaultdict(set)
    for c in movies:
        line = next((l for l in c.splitlines() if _norm_line(l) and _norm_line(l) not in lines), "")
        toks = normalize(clean_name(line)).split()
        for k in (1, 2, 3):
            if len(toks) > k:
                pre_count[tuple(toks[:k])] += 1
                pre_next[tuple(toks[:k])].add(" ".join(toks[k:k + 2]))
                suf_count[tuple(toks[-k:])] += 1
                suf_prev[tuple(toks[-k:])].add(" ".join(toks[max(0, len(toks) - k - 2):-k]))

    def pick(counts: Counter, variety: dict) -> list[list[str]]:
        found = [list(t) for t, k in counts.items() if k >= threshold and len(variety[t]) >= MIN_VARIETY]
        found = [t for t in found if not all(w.isdigit() for w in t)]  # never strip plain numbers / years
        return sorted(found, key=len, reverse=True)  # longest first

    return {"lines": sorted(lines), "prefixes": pick(pre_count, pre_next), "suffixes": pick(suf_count, suf_prev)}


def _words_regex(tokens: list[str]) -> str:
    # normalize() dropped quotes ("ע\"י" -> "עי"), so allow them between letters again
    def tok(t: str) -> str:
        return "['\"׳״]?".join(re.escape(ch) for ch in t)
    return r"[\W_]*".join(tok(t) for t in tokens)


def strip_caption(caption: str | None, branding: dict | None) -> str | None:
    """Remove signature lines; leave the rest of the caption."""
    if not caption or not branding or not branding.get("lines"):
        return caption
    lines = set(branding["lines"])
    kept = [l for l in caption.splitlines() if _norm_line(l) not in lines]
    return "\n".join(kept)


def _ok(rest: str) -> bool:
    """What is left after removing a signature must still look like a name."""
    n = normalize(rest)
    return len(n) > 1 and bool(re.search(r"[^\W\d_]", n))


def strip_name(name: str, branding: dict | None) -> str:
    if not branding:
        return name
    for toks in branding.get("prefixes", []):
        new = re.sub(r"^[\W_]*" + _words_regex(toks) + r"(?=[\W_]|$)[\W_]*", "", name, flags=re.I)
        if new != name and _ok(new):
            name = new
            break
    for toks in branding.get("suffixes", []):
        new = re.sub(r"[\W_]*(?<![\w])" + _words_regex(toks) + r"[\W_]*$", "", name, flags=re.I)
        if new != name and _ok(new):
            name = new
            break
    return name.strip(" -:|,–—")
