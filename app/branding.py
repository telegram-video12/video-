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

from .parser import EPISODE_PATTERNS, clean_name, normalize, words

VERSION = "9"  # bump when detection changes, so existing libraries are re-filed once
MIN_MOVIES = 20        # need this many movie captions in a channel before guessing
MIN_COUNT = 10         # a signature must appear at least this often...
MIN_SHARE = 0.005      # ...and in at least 0.5% of the channel's movies (big "archive" channels mix many sources)
MIN_VARIETY = 0.5      # ...and mostly in front of *different* names (so "ספיידרמן 2/3/..." is not a signature)


def _has_episode(text: str) -> bool:
    flat = re.sub(r"[._]+", " ", text)
    return any(p.search(flat) for p in EPISODE_PATTERNS)


def _norm_line(line: str) -> str:
    return words(clean_name(line))


YEAR_TOKEN = re.compile(r"^(?:19|20)\d\d$")
MIN_CONFIRMED = 0.15   # share of a prefix's movies that also appear under their plain name elsewhere


def _name_tokens(caption: str, lines: set) -> list[str]:
    line = next((l for l in caption.splitlines() if _norm_line(l) and _norm_line(l) not in lines), "")
    toks = words(clean_name(line)).split()
    while toks and YEAR_TOKEN.match(toks[-1]):
        toks.pop()
    return toks


def _same_length(a: float | None, b: float | None) -> bool:
    return bool(a and b and abs(a - b) <= max(120, 0.03 * max(a, b)))


def _same_movie(rest: str, a: float | None, b: float | None) -> bool:
    """Is the other upload the same movie? Same length when both lengths are known; when one is not
    (files sent as documents often have none), trust only a name of two words or more."""
    if a and b:
        return _same_length(a, b)
    return len(rest.split()) >= 2


def detect(captions: list, corpus: list | None = None) -> dict:
    """captions: one channel's (caption, duration) pairs. corpus: the same for the whole library.

    Durations tell re-uploads of the same movie ("קינג סרט X" / "X 1080p") apart from sequels
    ("X" / "X הנקמה"), so a real word is not mistaken for a signature.
    """
    captions = [c if isinstance(c, tuple) else (c, None) for c in captions]
    durations = {c: d for c, d in captions if c}
    movies = [c for c, _ in captions if c and c.strip() and not _has_episode(c)]
    n = len(movies)
    if n < MIN_MOVIES:
        return {}
    threshold = max(MIN_COUNT, n * MIN_SHARE)

    # 1. whole lines that repeat with different movies (e.g. "🎬 קינג סרט 🎬", "הצטרפו לערוץ!")
    line_count: Counter = Counter()
    line_with: dict = defaultdict(set)
    for c in movies:
        norm_lines = [x for x in (_norm_line(l) for l in c.splitlines()) if x]
        for nl in set(norm_lines):
            line_count[nl] += 1
            line_with[nl].add(next((o for o in norm_lines if o != nl), ""))
    lines = {l for l, k in line_count.items() if k >= threshold and len(line_with[l]) >= k * MIN_VARIETY}

    # 2. leading / trailing words. A real signature is confirmed by the same movies showing up
    #    elsewhere in the library *starting* (or ending) with the rest of the name, e.g.
    #    "קינג סרט ספיידרמן 3" here and "ספיידרמן 3 1080p" from another source.
    starts: dict = defaultdict(list)
    ends: dict = defaultdict(list)
    for c, dur in (corpus if corpus is not None else captions):
        if not c or _has_episode(c):
            continue
        toks = _name_tokens(c, lines)
        # the whole name, and the name minus up to 3 words at either end
        for k in range(max(1, len(toks) - 3), len(toks) + 1):
            starts[" ".join(toks[:k])].append(dur)
            ends[" ".join(toks[-k:])].append(dur)

    pre: dict = defaultdict(list)
    suf: dict = defaultdict(list)
    for c in movies:
        toks = _name_tokens(c, lines)
        for k in (1, 2, 3):
            if len(toks) > k:
                pre[tuple(toks[:k])].append((" ".join(toks[k:]), durations.get(c)))
                suf[tuple(toks[-k:])].append((" ".join(toks[:-k]), durations.get(c)))

    def pick(cands: dict, seen: dict) -> list[list[str]]:
        found = []
        for sig, rests in cands.items():
            k = len(rests)
            if k < threshold or all(w.isdigit() for w in sig):
                continue
            if len({r for r, _ in rests}) < k * MIN_VARIETY:
                continue
            confirmed = sum(1 for r, d in rests
                            if re.search(r"[^\W\d_]", r) and any(_same_movie(r, d, o) for o in seen.get(r, ())[:20]))
            if confirmed >= k * MIN_CONFIRMED:
                found.append(list(sig))
        # keep the shortest form: "דב ס" rather than also "2 דב ס" (which would eat the "2")
        found.sort(key=len)
        minimal = []
        for f in found:
            if not any(len(m) < len(f) and (f[:len(m)] == m or f[-len(m):] == m) for m in minimal):
                minimal.append(f)
        return sorted(minimal, key=len, reverse=True)

    return {"lines": sorted(lines), "prefixes": pick(pre, starts), "suffixes": pick(suf, ends)}


def _words_regex(tokens: list[str]) -> str:
    # normalize() dropped quotes ("ע\"י" -> "עי"), so allow them between letters again
    def tok(t: str) -> str:
        # words() also merged doubled ו/י ("טיים" -> "טים"), so allow them doubled again
        return "['\"׳״]?".join(re.escape(ch) + ("+" if ch in "יו" else "") for ch in t)
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
    for toks in branding.get("anywhere", []):  # phrases you listed in Settings
        new = re.sub(r"(?<![^\W_])" + _words_regex(toks) + r"(?![^\W_])", " ", name, flags=re.I)
        new = re.sub(r"\s+", " ", new).strip(" -:|,–—")
        if new != name and _ok(new):
            name = new
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


def combine(auto: dict | None, phrases: list[str]) -> dict:
    """Automatic signature of one channel + the phrases you listed in Settings (for every channel)."""
    out = dict(auto or {})
    out["anywhere"] = [t for t in (words(p).split() for p in phrases) if t]
    return out
