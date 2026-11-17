"""Guess series / season / episode / movie info from a Telegram caption or file name.

Handles common English patterns (S01E02, 1x02, Season 1 Episode 2) and
Hebrew ones ("עונה 1 פרק 2", "ע1 פ2", "פרק 5").
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HEB_NUM = r"(\d{1,4})"

# Each pattern must define named groups: season (optional) and episode.
EPISODE_PATTERNS = [
    re.compile(r"\bS(?P<season>\d{1,2})[\s._-]*E(?P<episode>\d{1,4})\b", re.I),
    re.compile(r"\b(?P<season>\d{1,2})x(?P<episode>\d{1,3})\b", re.I),
    re.compile(
        r"\bseason[\s._-]*(?P<season>\d{1,2})[\s._,-]*(?:episode|ep)[\s._-]*(?P<episode>\d{1,4})\b", re.I
    ),
    re.compile(r"עונה[\s._:-]*(?P<season>\d{1,2})[\s._,:-]*(?:-|,)?[\s]*פרק[\s._:-]*(?P<episode>\d{1,4})"),
    re.compile(r"(?<![א-ת])עו?['׳\"]?[\s._-]*(?P<season>\d{1,2})[\s._,-]*פר?['׳\"]?[\s._-]*(?P<episode>\d{1,4})"),
    re.compile(r"פרק[\s._:-]*(?P<episode>\d{1,4})[\s._,:-]*עונה[\s._:-]*(?P<season>\d{1,2})"),
    re.compile(r"\b(?:episode|ep)[\s._-]*(?P<episode>\d{1,4})\b", re.I),
    re.compile(r"פרק[\s._:-]*(?P<episode>\d{1,4})"),
    re.compile(r"\bE(?P<episode>\d{2,4})\b"),
]

SEASON_ONLY = [
    re.compile(r"\bseason[\s._-]*(\d{1,2})\b", re.I),
    re.compile(r"\bS(\d{1,2})\b"),
    re.compile(r"עונה[\s._:-]*(\d{1,2})"),
    re.compile(r"(?<![א-ת])עו['׳]?[\s._-]*(\d{1,2})\b"),
]

YEAR_RE = re.compile(r"[\(\[\s._-]((?:19|20)\d{2})(?:[\)\]\s._-]|$)")

JUNK_RE = re.compile(
    r"\b(2160p|1080p|720p|576p|480p|360p|4k|uhd|hdr|web[-. ]?dl|webrip|web|bluray|blu-ray|brrip|bdrip|"
    r"hdtv|dvdrip|x264|x265|h\.?264|h\.?265|hevc|avc|aac\d?(?:\.\d)?|ac3|dd5\.1|ddp?5\.1|10bit|"
    r"proper|repack|extended|unrated|remastered|multi|dual|subs?|hebsub|heb|eng|nf|amzn|dsnp|hmax|atvp|"
    r"מתורגם|תרגום|כתוביות|מדובב|באיכות|גבוהה|hd|sd|fhd|mkv|mp4|avi|"
    r"2160|1080|720|480|br|bd|dvd|dvdscr|vod|hdrip|hdcam|camrip|hdtc|1080i|\d{3,4}[pi])\b",
    re.I,
)

VIDEO_EXT_RE = re.compile(r"\.(mkv|mp4|avi|mov|m4v|webm|ts|wmv|flv)$", re.I)


@dataclass
class ParsedInfo:
    name: str
    kind: str  # "series" | "movie"
    season: int | None = None
    season_explicit: bool = False  # True when the text actually said which season
    episode: int | None = None
    year: int | None = None


UPLOADER_RE = re.compile(
    r"(?:הועל[הו]|עלה|תורגם|סונכרן|קידוד|הוכן)\s+(?:ע[\"״׳']?\s?י|על\s+ידי|באדיבות)\b.*$"  # "הועלה ע"י יחיאל ..."
    r"|באדיבות\s+\S+.*$"
    r"|\b(?:מדיה\s+)?VOD\b"
    r"|(?:ל)?צפי(?:י)?ה\s+ישירה"
    r"|\bfull\s*hd\b",
    re.I,
)


# Where a caption stops being the name and starts describing the movie
DESCRIPTION_RE = re.compile(
    r"(?:^|[\s\-–—|:•·,(])(?:ז['׳]?אנר(?:ים)?|סוגה|תקציר|עלילה|על\s+ה(?:סרט|סדרה)|תיאור|בימוי|במאי|שחקנים|"
    r"משתתפים|כיכוב|בכיכוב|שפה|אורך|דירוג|ציון|IMDb|genre|plot|synopsis|starring|director|cast)\b.*$",
    re.I | re.S,
)
MAX_NAME_WORDS = 9


def clean_name(text: str) -> str:
    text = VIDEO_EXT_RE.sub("", text)
    text = DESCRIPTION_RE.sub(" ", text)
    text = UPLOADER_RE.sub(" ", text)
    text = text.replace("#", " ")
    text = re.sub(r"@\w+", " ", text)  # channel mentions
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]", " ", text)  # bracketed tags
    text = re.sub(r"[._]+", " ", text)
    text = JUNK_RE.sub(" ", text)
    text = re.sub(r"[^\w\s'׳\"&!?:-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" -:|,–—")
    # uploader signatures at the start: "נתי מדיה X", "זירה מדיה X", "דב סרטים - X"
    text = re.sub(r"^[^\s\d]+\s+(?:מדיה|סרטים)\s*[-–:|]?\s+(?=\S)", "", text)
    # "הסרט: X" / "סדרה - X" -> "X"
    text = re.sub(r"^(?:הסרט|סרט|הסדרה|סדרה|movie|film)\s*[:\-–]?\s+(?=\S)", "", text, flags=re.I)
    words = text.split()
    if len(words) > MAX_NAME_WORDS:  # still a sentence: keep the part before " - " / the first words
        head = re.split(r"\s[-–—|]\s", text)[0].split()
        text = " ".join(head if 0 < len(head) <= MAX_NAME_WORDS else words[:MAX_NAME_WORDS // 2 + 1])
    return text.strip(" -:|,–—")


QUALITY_RE = re.compile(r"(?<!\d)(2160|1440|1080|720|576|480|360|240)[pi](?![a-z])", re.I)


def quality_from_text(*texts: str | None) -> int:
    """Highest resolution mentioned in a caption / file name, e.g. '1080p' -> 1080, '4K' -> 2160."""
    best = 0
    for t in texts:
        if not t:
            continue
        for m in QUALITY_RE.finditer(t):
            best = max(best, int(m.group(1)))
        if re.search(r"\b(4k|uhd)\b", t, re.I):
            best = max(best, 2160)
    return best


def words(name: str) -> str:
    """Lower-case words without punctuation; spelling variants unified ("הבייתה" = "הביתה")."""
    name = name.lower()
    name = re.sub(r"['׳\"]", "", name)
    name = re.sub(r"[^\w]+", " ", name)
    name = re.sub(r"\bthe\b", " ", name)
    name = re.sub(r"([יו])\1+", r"\1", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize(name: str) -> str:
    """Key used to group uploads of the same title: "ספיידר-מן" = "ספיידרמן", "Spider Man" = "Spiderman"."""
    return words(name).replace(" ", "")


def skeleton(key: str) -> str:
    """A normalized name without the letters Hebrew spelling adds or drops (ו, י, א, ה) after the first letter:
    "ספידרמן" and "ספיידרמן", "שטיסל" and "שטיסל" match. Used only for search."""
    return key[:1] + re.sub(r"[ויאה]", "", key[1:]) if key else ""


def _within_one_edit(q: str, text: str) -> bool:
    """Is q inside text with at most one wrong, missing or extra letter? (approximate substring search)"""
    n = len(q)
    prev = [0] * (len(text) + 1)  # a match may start anywhere in text
    for i in range(1, n + 1):
        cur = [i] + [0] * len(text)
        qi = q[i - 1]
        for j in range(1, len(text) + 1):
            cur[j] = min(prev[j - 1] + (qi != text[j - 1]), prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return min(prev) <= 1


def fuzzy_contains(q: str, key: str) -> bool:
    if len(q) < 4 or len(key) < len(q) - 1:
        return False
    letters = set(q)
    if sum(ch in key for ch in letters) < len(letters) - 1:  # quick check before the slower comparison
        return False
    return _within_one_edit(q, key)


def _first_meaningful_line(caption: str) -> str:
    for line in caption.splitlines():
        if re.search(r"[\wא-ת]", line):
            return line.strip()
    return ""


def _parse_one(text: str) -> ParsedInfo | None:
    """Try to parse a single line of text. Returns None when nothing useful found."""
    if not text:
        return None
    text = VIDEO_EXT_RE.sub("", text.strip())
    text = re.sub(r"[._]+", " ", text)
    for pattern in EPISODE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groupdict()
        episode = int(groups["episode"])
        season = int(groups["season"]) if groups.get("season") else None
        if season is None:
            for sp in SEASON_ONLY:
                sm = sp.search(text)
                if sm:
                    season = int(sm.group(1))
                    break
        name_part = text[: m.start()]
        # "Name - season 2" leftovers
        for sp in SEASON_ONLY:
            name_part = sp.sub(" ", name_part)
        name = clean_name(name_part)
        if not name:
            # name may come after the episode marker ("פרק 3 - שם הסדרה")
            name = clean_name(text[m.end():])
            name = re.sub(r"^\d+\s*", "", name)
        year = None
        ym = YEAR_RE.search(" " + name_part + " ")
        if ym:
            year = int(ym.group(1))
            name = clean_name(re.sub(rf"\b{year}\b", " ", name))
        if season is None:  # "הישרדות 8 פרק 3": a small number right after the name is the season
            sm = re.fullmatch(r"(.*\S)\s+(\d{1,2})", name)
            if sm and 1 <= int(sm.group(2)) <= 40 and re.search(r"[^\d\s]", sm.group(1)):
                name, season = sm.group(1).strip(" -:|,–—"), int(sm.group(2))
        return ParsedInfo(name=name, kind="series", season=season or 1, episode=episode, year=year,
                          season_explicit=season is not None)

    # No episode marker -> movie
    year = None
    name_text = text
    ym = YEAR_RE.search(" " + text + " ")
    if ym:
        year = int(ym.group(1))
        # Everything after the year is usually release junk
        idx = text.find(ym.group(1))
        name_text = text[:idx]
    name = clean_name(name_text)
    if not name:
        return None
    return ParsedInfo(name=name, kind="movie", year=year)


def parse_media(caption: str | None, file_name: str | None, fallback_name: str = "",
                branding: dict | None = None) -> ParsedInfo:
    """branding: the channel's own signature (see branding.py), removed before parsing."""
    if branding:
        from .branding import strip_caption
        caption = strip_caption(caption, branding)
    caption = (caption or "").strip()
    file_name = (file_name or "").strip()

    candidates = []
    if caption:
        candidates.append(_parse_one(_first_meaningful_line(caption)))
        candidates.append(_parse_one(caption.replace("\n", " ")))
    if file_name:
        candidates.append(_parse_one(file_name))
    candidates = [c for c in candidates if c]

    # Prefer anything that found an episode; then anything with a name.
    best = next((c for c in candidates if c.kind == "series" and c.name), None)
    if best is None:
        series_no_name = next((c for c in candidates if c.kind == "series"), None)
        if series_no_name:
            named = next((c for c in candidates if c.name), None)
            series_no_name.name = (named.name if named else "") or fallback_name
            best = series_no_name
    if best is None:
        best = next((c for c in candidates if c.name), None)
    if best is None:
        best = ParsedInfo(name=fallback_name or "ללא שם", kind="movie")
    if branding:
        from .branding import strip_name
        best.name = strip_name(best.name, branding)
    if not best.name:
        best.name = fallback_name or "ללא שם"
    return best
