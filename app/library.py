"""Turns a Telegram video message into a library entry (title + media row)."""
from __future__ import annotations

import json
import logging
import time

from . import branding as brand
from .db import Database
from .parser import normalize, parse_media, words

log = logging.getLogger(__name__)
MIN_UNKNOWN_DURATION_SIZE = 50 * 1024 * 1024  # files without duration: keep only if >= 50MB


def _stored(chat: dict) -> dict | None:
    try:
        return json.loads(chat.get("branding") or "null")
    except ValueError:
        return None


def chat_branding(chat: dict, db: Database | None = None) -> dict | None:
    """Signature removed from this channel's names: learned automatically + your phrases from Settings."""
    auto = _stored(chat)
    phrases = db.strip_phrases() if db is not None else []
    if not auto and not phrases:
        return None
    return brand.combine(auto, phrases)


def add_video(db: Database, v, chat: dict | str, min_duration: int = 0) -> bool:
    """Returns True when a new media row was created."""
    if min_duration:
        if v.duration:
            if v.duration < min_duration:
                return False
        elif v.size < MIN_UNKNOWN_DURATION_SIZE:
            return False
    if isinstance(chat, str):
        chat = {"title": chat}
    if db.has_media(v.chat_id, v.msg_id):  # already in the library (e.g. a full re-scan): nothing to do
        return False
    if "_branding" not in chat:  # the same for every video of this group: work it out once per scan
        chat["_branding"] = chat_branding(chat, db)
    info = parse_media(v.caption, v.file_name, fallback_name=chat.get("title", ""), branding=chat["_branding"])
    if info.kind == "series" and not info.season_explicit and chat.get("default_season"):
        info.season = chat["default_season"]
    norm = normalize(info.name) or info.name
    title_id = db.get_or_create_title(info.kind, info.name, norm, info.year, soft=True)
    new_id = db.add_media(
        _soft=True,
        auto_norm=norm,
        chat_id=v.chat_id,
        msg_id=v.msg_id,
        title_id=title_id,
        season=info.season,
        episode=info.episode,
        file_name=v.file_name,
        caption=v.caption,
        size=v.size,
        mime=v.mime,
        duration=v.duration,
        width=v.width,
        height=v.height,
        added_at=v.date,
    )
    if new_id is None:
        db.delete_empty_titles()
    return new_id is not None


def _corpus(db: Database) -> list:
    return [(r["caption"], r["duration"]) for r in db.q("SELECT caption, duration FROM media")]


def regroup_chat(db: Database, chat_id: int, corpus: list | None = None, force: bool = False) -> int:
    """Learn the channel's signature from all its captions and re-file its videos under clean names.

    Only videos that still sit where the automatic parser put them are moved, so manual
    fixes, merges (yours or TMDB's) are kept. Returns how many videos moved.
    """
    chat = db.get_chat(chat_id)
    if not chat:
        return 0
    rows = db.q("SELECT m.id, m.title_id, m.caption, m.file_name, m.manual, m.duration, m.auto_norm, "
                "t.kind, t.norm_name, t.tmdb_id FROM media m "
                "JOIN titles t ON t.id = m.title_id WHERE m.chat_id = ?", (chat_id,))
    old = _stored(chat)  # what was used last time (auto + phrases)
    if corpus is None:
        corpus = _corpus(db)
    new = brand.combine(brand.detect([(r["caption"], r["duration"]) for r in rows], corpus), db.strip_phrases())
    db.x("UPDATE chats SET branding = ? WHERE id = ?", (json.dumps(new, ensure_ascii=False), chat_id))
    if new == old and not force:
        return 0
    started = time.time()
    # 1. work out the moves (slow part, no lock held so the site stays responsive)
    moves = []
    for r in rows:
        if r["manual"]:
            continue
        fallback = chat["title"]
        if r["auto_norm"] is not None and r["auto_norm"] != r["norm_name"] and r["tmdb_id"] is not None:
            continue  # TMDB merged it into an entry with another name: leave it there
        info = parse_media(r["caption"], r["file_name"], fallback, new)
        norm = normalize(info.name) or info.name
        if info.kind != r["kind"] or norm != r["norm_name"]:
            # a season found in the name ("הישרדות 8 פרק 3") moves along with it
            season = info.season if info.kind == "series" and info.season_explicit else None
            moves.append((r["id"], r["title_id"], info.kind, norm, info.name, info.year, season))
    if not moves:
        return 0
    # 2. apply them in one transaction
    cache: dict[tuple[str, str], int] = {}
    with db._lock:
        conn = db.conn
        for media_id, _current, kind, norm, name, year, season in moves:
            key = (kind, norm, year)
            if key not in cache:
                tid = db.find_title(conn, kind, norm, year)
                cache[key] = tid or conn.execute(
                    "INSERT INTO titles (kind, name, norm_name, name_key, year, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, name, db.free_key(conn, kind, norm, year), normalize(name), year, time.time())).lastrowid
            conn.execute("UPDATE media SET title_id = ?, auto_norm = ?, season = COALESCE(?, season) WHERE id = ?",
                         (cache[key], norm, season, media_id))
        conn.execute("DELETE FROM titles WHERE id NOT IN (SELECT DISTINCT title_id FROM media)")
        conn.commit()
        db.version += 1
    log.info("chat %s: %d videos re-filed under clean names (%.1fs)", chat_id, len(moves), time.time() - started)
    return len(moves)


# ------------------------------------------------------------ same title, extra words
def _title_rows(db: Database) -> list[dict]:
    rows = db.q("""SELECT t.id, t.kind, t.name, t.year, t.tmdb_id, t.poster,
                          MAX(m.duration) AS duration, MAX(m.manual) AS manual, COUNT(m.id) AS files
                   FROM titles t JOIN media m ON m.title_id = t.id AND m.hidden = 0 GROUP BY t.id""")
    for r in rows:
        r["_w"] = tuple(words(r["name"] or "").split())
    return rows


def _num(word: str) -> bool:
    return any(ch.isdigit() for ch in word)


def _years_ok(a: dict, b: dict) -> bool:
    return not (a["year"] and b["year"] and abs(a["year"] - b["year"]) > 1)


def _contained_pairs(rows: list[dict]) -> list[dict]:
    """A title whose name CONTAINS another title's whole name, with extra words before and/or after:
    "חן סרטים אבא גנוב 2 ישראלי הועלה" contains "אבא גנוב 2". Rules that keep different titles apart:
    the inner name has at least two words; no number right before or after it ("אבא גנוב" is not inside
    "אבא גנוב 2"), except "1" right after a name without a number ("אבא גנוב 1" = TMDB's "אבא גנוב")."""
    index: dict[tuple, list[dict]] = {}
    for r in rows:
        w = r["_w"]
        if len(w) >= 2:
            index.setdefault((r["kind"], w), []).append(r)
            if not _num(w[-1]):  # the first movie of a series is often named without "1"
                index.setdefault((r["kind"], w + ("1",)), []).append(r)
    pairs, seen = [], set()
    for host in rows:
        w = host["_w"]
        n = len(w)
        if n < 3:
            continue
        rank = 0
        for size in range(n, 1, -1):  # longer inner names first (all of it: "אבא גנוב 1" = "אבא גנוב")
            for i in range(0, n - size + 1):
                inner = w[i:i + size]
                before, after = w[:i], w[i + size:]
                if (before and _num(before[-1])) or (after and _num(after[0])):
                    continue
                for t in index.get((host["kind"], inner), []):
                    if t["id"] == host["id"] or len(t["_w"]) >= n or not _years_ok(t, host):
                        continue
                    if (t["id"], host["id"]) in seen:
                        continue
                    seen.add((t["id"], host["id"]))
                    pairs.append({"short": t, "long": host, "rank": rank,
                                  "extras": [x for x in (" ".join(before), " ".join(after)) if x]})
                    rank += 1
    return pairs  # every candidate of a host, best (longest inner name) first


def _similar_pairs(db: Database, rows: list[dict] | None = None) -> list[dict]:
    """Titles whose name is another title's name plus one or two extra words at the start or the end
    ("אבא גנוב 1" / "אבא גנוב 1 גוזלן"). Extra words with digits never count ("אבא גנוב" / "אבא גנוב 2")."""
    rows = rows if rows is not None else _title_rows(db)
    index: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["_w"]:
            index.setdefault((r["kind"], r["_w"]), []).append(r)
    pairs, seen = [], set()
    for r in rows:
        w = r["_w"]
        if len(w) < 2:
            continue
        for cut in (-1, -2, 1, 2):
            short, extra = (w[:cut], w[cut:]) if cut < 0 else (w[cut:], w[:cut])
            if not short or len("".join(short)) < 2 or any(_num(x) for x in extra):
                continue
            for o in index.get((r["kind"], short), []):
                if o["id"] == r["id"] or (o["id"], r["id"]) in seen or not _years_ok(o, r):
                    continue
                if o["tmdb_id"] and r["tmdb_id"] and o["tmdb_id"] != r["tmdb_id"]:
                    continue
                seen.add((o["id"], r["id"]))
                pairs.append({"short": o, "long": r, "extra": " ".join(extra), "extras": [" ".join(extra)]})
    return pairs


def _same_length(a: dict, b: dict, ratio: float = 0.03, floor: float = 90) -> bool:
    da, db_ = a["duration"], b["duration"]
    return bool(da and db_ and abs(da - db_) <= max(ratio * max(da, db_), floor))


def merge_similar_movies(db: Database) -> int:
    """Join uploads of the same movie that carry extra words in the name (all uploads stay inside as
    versions). 1) one extra word at the start or end; 2) the name of a movie already in the library inside
    a longer name ("חן סרטים אבא גנוב 2 ישראלי הועלה"). Always only with a file of the same length.
    Series are only suggested (episodes of different series have similar lengths)."""
    merged = 0
    rows = _title_rows(db)
    for p in _similar_pairs(db, rows):
        a, b = p["short"], p["long"]
        if a["kind"] != "movie" or a["manual"] or b["manual"] or not _same_length(a, b):
            continue
        # Only ONE extra word, and TMDB must not know the longer name as a movie of its own
        # ("הנוקמים" / "הנוקמים עידן אולטרון" are two movies of almost the same length).
        if len(p["extra"].split()) != 1 or (b["tmdb_id"] and b["tmdb_id"] != a["tmdb_id"]):
            continue
        if not db.get_title(a["id"]) or not db.get_title(b["id"]):
            continue  # one of them was joined already in this round
        db.merge_titles(b["id"], a["id"])
        merged += 1
    rows = _title_rows(db)
    joined: set[int] = set()
    for p in _contained_pairs(rows):
        t, host = p["short"], p["long"]
        if host["id"] in joined or t["id"] in joined:
            continue
        if t["kind"] != "movie" or host["tmdb_id"] or host["manual"] or not _same_length(t, host, 0.02, 90):
            continue  # TMDB knows the longer name as its own movie, or the length differs: try the next one
        if not db.get_title(t["id"]) or not db.get_title(host["id"]):
            continue
        db.merge_titles(host["id"], t["id"])
        joined.add(host["id"])
        merged += 1
    if merged:
        log.info("%d movies with extra words in the name joined with the same movie", merged)
    return merged


def possible_duplicates(db: Database) -> list[dict]:
    """What is left for you to decide: looks like the same title but was not joined automatically.
    Remembered until the library changes (Settings asks for the count often)."""
    stamp = (db.version, db.soft_version, db.get_pref("dup_dismissed"))
    cached = getattr(db, "_dups_cache", None)
    if cached and cached[0] == stamp:
        return cached[1]
    out = _possible_duplicates(db)
    db._dups_cache = (stamp, out)
    return out


def _possible_duplicates(db: Database) -> list[dict]:
    try:
        dismissed = set(json.loads(db.get_pref("dup_dismissed") or "[]"))
    except ValueError:
        dismissed = set()
    rows = _title_rows(db)
    out, keys = [], set()
    contained = sorted(_contained_pairs(rows), key=lambda p: (not p["short"]["tmdb_id"], p["rank"]))
    hosts: set[int] = set()
    for p in _similar_pairs(db, rows) + contained:
        a, b = p["short"], p["long"]
        key = f"{min(a['id'], b['id'])}-{max(a['id'], b['id'])}"
        if key in dismissed or key in keys or ("rank" in p and b["id"] in hosts):
            continue  # one suggestion per long name: the best match (a TMDB title, the longest name)
        keys.add(key)
        hosts.add(b["id"])
        out.append({"key": key, "kind": a["kind"], "extra": " … ".join(p["extras"]), "extras": p["extras"],
                    "a": {k: a[k] for k in ("id", "name", "year", "poster", "files", "duration")},
                    "b": {k: b[k] for k in ("id", "name", "year", "poster", "files", "duration")}})
    return out


def refresh_series_years(db: Database) -> None:
    """Once: series already matched on TMDB fetch their details again to learn their latest season's year."""
    if db.get_pref("series_last_year") == "1":
        return
    db.x("UPDATE titles SET tmdb_details = 0 WHERE kind = 'series' AND tmdb_id IS NOT NULL", soft=True)
    db.set_pref("series_last_year", "1")


def retry_unmatched(db: Database) -> None:
    """Once after the smarter TMDB search arrived: look again for titles TMDB did not find before."""
    if db.get_pref("tmdb_retry") == "smart1":
        return
    db.x("UPDATE titles SET tmdb_checked = 0 WHERE tmdb_id IS NULL", soft=True)
    db.set_pref("tmdb_retry", "smart1")


def regroup_all(db: Database, force: bool = False, only: set | None = None) -> int:
    """Re-learn every channel's signature (the whole library is used to confirm them).
    force: also re-file when only the name parser itself changed (after an update).
    only: just these groups (the ones a scan found new videos in)."""
    corpus = _corpus(db)
    moved = sum(regroup_chat(db, c["id"], corpus, force) for c in db.chats() if only is None or c["id"] in only)
    moved += db.merge_by_name()
    moved += merge_similar_movies(db)
    db.x("UPDATE media SET auto_norm = (SELECT norm_name FROM titles t WHERE t.id = media.title_id) "
         "WHERE auto_norm IS NULL AND manual = 0")
    repair(db)
    return moved


def repair(db: Database) -> int:
    """Give videos that lost their title (e.g. an interrupted update) a title again."""
    fixed = 0
    for m in db.orphan_media():
        chat = db.get_chat(m["chat_id"]) or {"title": ""}
        info = parse_media(m["caption"], m["file_name"], chat.get("title", ""), chat_branding(chat, db))
        norm = normalize(info.name) or info.name
        title_id = db.get_or_create_title(info.kind, info.name, norm, info.year)
        db.update_media(m["id"], title_id=title_id)
        fixed += 1
    if fixed:
        log.warning("repaired %d videos without a title", fixed)
    return fixed


NORM_VERSION = "2"  # bump when parser.normalize() changes


def migrate_norms(db: Database) -> int:
    """Re-key existing titles after normalize() changed; titles that now share a key are merged
    (the one with TMDB info is kept), so posters, progress and edits are not lost."""
    if db.get_pref("norm_version") == NORM_VERSION:
        return 0
    started, merged = time.time(), 0
    with db._lock:
        conn = db.conn
        groups: dict[tuple[str, str], list] = {}
        for tid, kind, norm, tmdb in conn.execute("SELECT id, kind, norm_name, tmdb_id FROM titles"):
            groups.setdefault((kind, normalize(norm) or norm), []).append((tid, norm, tmdb))
        for (kind, key), rows in groups.items():
            rows.sort(key=lambda r: (r[2] is None, r[0]))  # keep a title that has TMDB info, else the oldest
            dst = rows[0][0]
            for tid, _, _ in rows[1:]:
                conn.execute("UPDATE media SET title_id = ? WHERE title_id = ?", (dst, tid))
                conn.execute("DELETE FROM titles WHERE id = ?", (tid,))
                merged += 1
            if rows[0][1] != key:
                conn.execute("UPDATE titles SET norm_name = ? WHERE id = ?", (key, dst))
        keys: dict[str, str] = {}
        changes = []
        for mid, old in conn.execute("SELECT id, auto_norm FROM media WHERE auto_norm IS NOT NULL").fetchall():
            new = keys.get(old) or keys.setdefault(old, normalize(old) or old)
            if new != old:
                changes.append((new, mid))
        conn.executemany("UPDATE media SET auto_norm = ? WHERE id = ?", changes)  # by id: no full scans
        conn.commit()
        db.version += 1
    db.set_pref("norm_version", NORM_VERSION)
    log.info("name keys updated: %d duplicate titles merged (%.1fs)", merged, time.time() - started)
    return merged
