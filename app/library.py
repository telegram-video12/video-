"""Turns a Telegram video message into a library entry (title + media row)."""
from __future__ import annotations

import json
import logging
import time

from . import branding as brand
from .db import Database
from .parser import normalize, parse_media

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
    info = parse_media(v.caption, v.file_name, fallback_name=chat.get("title", ""), branding=chat_branding(chat, db))
    if info.kind == "series" and not info.season_explicit and chat.get("default_season"):
        info.season = chat["default_season"]
    norm = normalize(info.name) or info.name
    title_id = db.get_or_create_title(info.kind, info.name, norm, info.year)
    new_id = db.add_media(
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
    rows = db.q("SELECT m.id, m.caption, m.file_name, m.manual, m.duration, m.auto_norm, t.kind, t.norm_name FROM media m "
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
        if r["auto_norm"] is not None and r["auto_norm"] != r["norm_name"]:
            continue  # merged or moved since it was added: leave it alone
        info = parse_media(r["caption"], r["file_name"], fallback, new)
        norm = normalize(info.name) or info.name
        if info.kind != r["kind"] or norm != r["norm_name"]:
            moves.append((r["id"], info.kind, norm, info.name, info.year))
    if not moves:
        return 0
    # 2. apply them in one transaction
    cache: dict[tuple[str, str], int] = {}
    with db._lock:
        conn = db.conn
        for media_id, kind, norm, name, year in moves:
            key = (kind, norm)
            if key not in cache:
                row = conn.execute("SELECT id FROM titles WHERE kind = ? AND norm_name = ?", key).fetchone()
                cache[key] = row[0] if row else conn.execute(
                    "INSERT INTO titles (kind, name, norm_name, year, created_at) VALUES (?, ?, ?, ?, ?)",
                    (kind, name, norm, year, time.time())).lastrowid
            conn.execute("UPDATE media SET title_id = ?, auto_norm = ? WHERE id = ?", (cache[key], norm, media_id))
        conn.execute("DELETE FROM titles WHERE id NOT IN (SELECT DISTINCT title_id FROM media)")
        conn.commit()
        db.version += 1
    log.info("chat %s: %d videos re-filed under clean names (%.1fs)", chat_id, len(moves), time.time() - started)
    return len(moves)


def regroup_all(db: Database, force: bool = False) -> int:
    """Re-learn every channel's signature (the whole library is used to confirm them).
    force: also re-file when only the name parser itself changed (after an update)."""
    corpus = _corpus(db)
    moved = sum(regroup_chat(db, c["id"], corpus, force) for c in db.chats())
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
