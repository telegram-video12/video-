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


def chat_branding(chat: dict) -> dict | None:
    try:
        return json.loads(chat.get("branding") or "null")
    except ValueError:
        return None


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
    info = parse_media(v.caption, v.file_name, fallback_name=chat.get("title", ""), branding=chat_branding(chat))
    if info.kind == "series" and not info.season_explicit and chat.get("default_season"):
        info.season = chat["default_season"]
    title_id = db.get_or_create_title(info.kind, info.name, normalize(info.name) or info.name, info.year)
    new_id = db.add_media(
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


def regroup_chat(db: Database, chat_id: int) -> int:
    """Learn the channel's signature from all its captions and re-file its videos under clean names.

    Only videos that still sit where the automatic parser put them are moved, so manual
    fixes, merges (yours or TMDB's) are kept. Returns how many videos moved.
    """
    chat = db.get_chat(chat_id)
    if not chat:
        return 0
    rows = db.q("SELECT m.id, m.caption, m.file_name, m.manual, t.kind, t.norm_name FROM media m "
                "JOIN titles t ON t.id = m.title_id WHERE m.chat_id = ?", (chat_id,))
    old = chat_branding(chat)
    new = brand.detect([r["caption"] for r in rows])
    db.x("UPDATE chats SET branding = ? WHERE id = ?", (json.dumps(new, ensure_ascii=False), chat_id))
    if new == old:
        return 0
    started = time.time()
    # 1. work out the moves (slow part, no lock held so the site stays responsive)
    moves = []
    for r in rows:
        if r["manual"]:
            continue
        fallback = chat["title"]
        before = {normalize(parse_media(r["caption"], r["file_name"], fallback, b).name) for b in (None, old)}
        if r["norm_name"] not in before:
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
            conn.execute("UPDATE media SET title_id = ? WHERE id = ?", (cache[key], media_id))
        conn.execute("DELETE FROM titles WHERE id NOT IN (SELECT DISTINCT title_id FROM media)")
        conn.commit()
        db.version += 1
    log.info("chat %s: %d videos re-filed under clean names (%.1fs)", chat_id, len(moves), time.time() - started)
    return len(moves)
