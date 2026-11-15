"""Turns a Telegram video message into a library entry (title + media row)."""
from __future__ import annotations

from .db import Database
from .parser import normalize, parse_media

MIN_UNKNOWN_DURATION_SIZE = 50 * 1024 * 1024  # files without duration: keep only if >= 50MB


def add_video(db: Database, v, chat_title: str, min_duration: int = 0) -> bool:
    """Returns True when a new media row was created."""
    if min_duration:
        if v.duration:
            if v.duration < min_duration:
                return False
        elif v.size < MIN_UNKNOWN_DURATION_SIZE:
            return False
    info = parse_media(v.caption, v.file_name, fallback_name=chat_title)
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
