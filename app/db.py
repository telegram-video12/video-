"""SQLite storage: chats, titles (series/movies), media files and watch progress."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY,           -- Telegram chat id (marked, e.g. -100...)
    title       TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_msg_id INTEGER NOT NULL DEFAULT 0,
    last_scan   REAL
);

CREATE TABLE IF NOT EXISTS titles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('series', 'movie')),
    name         TEXT NOT NULL,
    norm_name    TEXT NOT NULL,
    year         INTEGER,
    overview     TEXT,
    poster       TEXT,
    backdrop     TEXT,
    tmdb_id      INTEGER,
    tmdb_checked INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    UNIQUE (kind, norm_name)
);

CREATE TABLE IF NOT EXISTS media (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    title_id  INTEGER NOT NULL REFERENCES titles(id),
    season    INTEGER,
    episode   INTEGER,
    file_name TEXT,
    caption   TEXT,
    size      INTEGER NOT NULL DEFAULT 0,
    mime      TEXT,
    duration  REAL,
    width     INTEGER,
    height    INTEGER,
    hidden    INTEGER NOT NULL DEFAULT 0,
    added_at  REAL NOT NULL,
    UNIQUE (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS media_title ON media(title_id, season, episode);

CREATE TABLE IF NOT EXISTS progress (
    media_id   INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    position   REAL NOT NULL DEFAULT 0,
    duration   REAL,
    completed  INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""

# Several uploads of the same episode/movie (other groups, other qualities) count as one.
EP_KEY = ("(CASE WHEN t.kind = 'movie' THEN 'movie' WHEN m.episode IS NULL THEN 'id' || m.id "
          "ELSE m.season || ':' || m.episode END)")

COMPLETE_RATIO = 0.92  # watched this much -> episode counts as finished
MIN_STARTED = 10  # seconds; peeking at an episode for less than this does not count as watching it


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(chats)")}
        if "default_season" not in cols:
            self.conn.execute("ALTER TABLE chats ADD COLUMN default_season INTEGER")
        if "season_label" not in cols:
            self.conn.execute("ALTER TABLE chats ADD COLUMN season_label TEXT")

    # ---------------------------------------------------------------- helpers
    def q(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def q1(self, sql: str, args: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple | list = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    # ------------------------------------------------------------------ chats
    def upsert_chat(self, chat_id: int, title: str, enabled: bool,
                    default_season: int | None = None, season_label: str | None = None) -> None:
        self.x(
            """INSERT INTO chats (id, title, enabled, default_season, season_label) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title = excluded.title, enabled = excluded.enabled,
                 default_season = excluded.default_season, season_label = excluded.season_label""",
            (chat_id, title, int(enabled), default_season, season_label or None),
        )

    def get_chat(self, chat_id: int) -> dict | None:
        return self.q1("SELECT * FROM chats WHERE id = ?", (chat_id,))

    def apply_chat_season(self, chat_id: int) -> int:
        """Re-assign seasons of already-scanned episodes from this chat whose text named no season."""
        from .parser import parse_media

        chat = self.get_chat(chat_id)
        if not chat:
            return 0
        changed = 0
        for m in self.q("SELECT id, caption, file_name, season, episode FROM media WHERE chat_id = ?", (chat_id,)):
            if m["episode"] is None:
                continue
            info = parse_media(m["caption"], m["file_name"], fallback_name=chat["title"])
            if info.kind != "series" or info.season_explicit:
                continue
            season = chat["default_season"] or info.season or 1
            if season != m["season"]:
                self.update_media(m["id"], season=season)
                changed += 1
        return changed

    def season_labels(self, title_id: int) -> dict[int, str]:
        rows = self.q(
            """SELECT DISTINCT m.season, c.season_label FROM media m JOIN chats c ON c.id = m.chat_id
               WHERE m.title_id = ? AND c.default_season = m.season AND c.season_label IS NOT NULL""",
            (title_id,),
        )
        return {r["season"]: r["season_label"] for r in rows}

    def chats(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM chats" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY title"
        return self.q(sql)

    def set_chat_scanned(self, chat_id: int, last_msg_id: int) -> None:
        self.x(
            "UPDATE chats SET last_msg_id = MAX(last_msg_id, ?), last_scan = ? WHERE id = ?",
            (last_msg_id, time.time(), chat_id),
        )

    def reset_chat(self, chat_id: int) -> None:
        self.x("UPDATE chats SET last_msg_id = 0 WHERE id = ?", (chat_id,))

    # ----------------------------------------------------------------- titles
    def get_or_create_title(self, kind: str, name: str, norm_name: str, year: int | None) -> int:
        row = self.q1("SELECT id FROM titles WHERE kind = ? AND norm_name = ?", (kind, norm_name))
        if row:
            return row["id"]
        return self.x(
            "INSERT INTO titles (kind, name, norm_name, year, created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, name, norm_name, year, time.time()),
        )

    def update_title(self, title_id: int, **fields: Any) -> None:
        allowed = {"kind", "name", "norm_name", "year", "overview", "poster", "backdrop", "tmdb_id", "tmdb_checked"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.x(f"UPDATE titles SET {sets} WHERE id = ?", (*fields.values(), title_id))

    def merge_titles(self, source_id: int, target_id: int) -> None:
        with self._lock:
            self.conn.execute("UPDATE media SET title_id = ? WHERE title_id = ?", (target_id, source_id))
            self.conn.execute("DELETE FROM titles WHERE id = ?", (source_id,))
            self.conn.commit()

    def delete_empty_titles(self) -> None:
        self.x("DELETE FROM titles WHERE id NOT IN (SELECT DISTINCT title_id FROM media)")

    def titles_needing_tmdb(self) -> list[dict]:
        return self.q("SELECT * FROM titles WHERE tmdb_checked = 0")

    def list_titles(self) -> list[dict]:
        return self.q(
            f"""
            SELECT t.id, t.kind, t.name, t.year, t.poster, t.overview,
                   COUNT(DISTINCT {EP_KEY}) AS media_count,
                   COUNT(DISTINCT m.season) AS season_count,
                   MAX(m.added_at) AS last_added,
                   MIN(m.id) AS first_media_id,
                   COUNT(DISTINCT CASE WHEN p.completed = 1 THEN {EP_KEY} END) AS watched_count,
                   MAX(p.updated_at) AS last_watched
            FROM titles t
            JOIN media m ON m.title_id = t.id AND m.hidden = 0
            LEFT JOIN progress p ON p.media_id = m.id
            GROUP BY t.id
            ORDER BY last_added DESC
            """
        )

    def get_title(self, title_id: int) -> dict | None:
        return self.q1("SELECT * FROM titles WHERE id = ?", (title_id,))

    # ------------------------------------------------------------------ media
    def add_media(self, **m: Any) -> int | None:
        """Insert a media row; existing (chat_id, msg_id) rows are left untouched so manual edits survive."""
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO media
                   (chat_id, msg_id, title_id, season, episode, file_name, caption, size, mime,
                    duration, width, height, added_at)
                   VALUES (:chat_id, :msg_id, :title_id, :season, :episode, :file_name, :caption, :size,
                           :mime, :duration, :width, :height, :added_at)""",
                m,
            )
            self.conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def get_media(self, media_id: int) -> dict | None:
        return self.q1(
            """SELECT m.*, p.position, p.completed, p.updated_at AS watched_at
               FROM media m LEFT JOIN progress p ON p.media_id = m.id WHERE m.id = ?""",
            (media_id,),
        )

    def update_media(self, media_id: int, **fields: Any) -> None:
        allowed = {"title_id", "season", "episode", "hidden"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.x(f"UPDATE media SET {sets} WHERE id = ?", (*fields.values(), media_id))

    def _all_media(self, title_id: int, include_hidden: bool) -> tuple[str, list[dict]]:
        hidden = "" if include_hidden else "AND m.hidden = 0"
        rows = self.q(
            f"""SELECT m.*, t.kind, p.position, p.duration AS watched_duration, p.completed,
                       p.updated_at AS watched_at
                FROM media m JOIN titles t ON t.id = m.title_id
                LEFT JOIN progress p ON p.media_id = m.id
                WHERE m.title_id = ? {hidden}
                ORDER BY COALESCE(m.season, 0), COALESCE(m.episode, 0), m.chat_id, m.msg_id""",
            (title_id,),
        )
        for r in rows:
            r["quality"] = quality_of(r)
        return (rows[0]["kind"] if rows else "series"), rows

    @staticmethod
    def episode_key(m: dict, kind: str) -> tuple:
        if kind == "movie":
            return ("movie",)
        if m["episode"] is None:
            return ("id", m["id"])
        return (m["season"], m["episode"])

    def _grouped(self, title_id: int) -> list[list[dict]]:
        """Versions of the same episode grouped together, best quality first."""
        kind, rows = self._all_media(title_id, False)
        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            groups.setdefault(self.episode_key(r, kind), []).append(r)
        out = []
        for versions in groups.values():
            versions.sort(key=lambda r: (r["quality"], r["size"] or 0, -r["msg_id"]), reverse=True)
            out.append(versions)
        return out

    def title_media(self, title_id: int, include_hidden: bool = False) -> list[dict]:
        """One row per episode (the best quality upload) unless include_hidden, which lists every file.

        Watch progress is shared between versions: the most recently watched one wins.
        """
        if include_hidden:
            rows = self._all_media(title_id, True)[1]
            for r in rows:
                r["versions"] = 1
            return rows
        out = []
        for versions in self._grouped(title_id):
            best = dict(versions[0])
            watched = [v for v in versions if v.get("watched_at")]
            if watched:
                last = max(watched, key=lambda v: v["watched_at"])
                for k in ("position", "watched_duration", "completed", "watched_at"):
                    best[k] = last[k]
                best["completed"] = max(v["completed"] or 0 for v in watched)
            best["versions"] = len(versions)
            out.append(best)
        return out

    def versions(self, media_id: int) -> list[dict]:
        m = self.get_media(media_id)
        if not m:
            return []
        for group in self._grouped(m["title_id"]):
            if any(v["id"] == media_id for v in group):
                return group
        return [dict(m, quality=quality_of(m))]

    # --------------------------------------------------------------- progress
    def save_progress(self, media_id: int, position: float, duration: float | None) -> bool:
        media = self.get_media(media_id)
        if not media:
            return False
        duration = duration or media.get("duration")
        completed = bool(duration and position >= duration * COMPLETE_RATIO)
        self.x(
            """INSERT INTO progress (media_id, position, duration, completed, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(media_id) DO UPDATE SET position = excluded.position,
                 duration = excluded.duration,
                 completed = MAX(progress.completed, excluded.completed),
                 updated_at = excluded.updated_at""",
            (media_id, position, duration, int(completed), time.time()),
        )
        return completed

    def mark_watched(self, media_id: int, watched: bool) -> None:
        if watched:
            self.x(
                """INSERT INTO progress (media_id, position, completed, updated_at) VALUES (?, 0, 1, ?)
                   ON CONFLICT(media_id) DO UPDATE SET completed = 1, position = 0, updated_at = excluded.updated_at""",
                (media_id, time.time()),
            )
        else:
            self.x("DELETE FROM progress WHERE media_id = ?", (media_id,))

    # ----------------------------------------------------- continue watching
    def resume_point(self, title_id: int) -> dict | None:
        """Which media should "continue" open for this title, and from where."""
        items = self.title_media(title_id)
        if not items:
            return None
        watched = [m for m in items if m.get("watched_at") and (m["completed"] or (m["position"] or 0) >= MIN_STARTED)]
        if not watched:
            return {"media": items[0], "position": 0, "state": "start"}
        last = max(watched, key=lambda m: m["watched_at"])
        if not last["completed"]:
            return {"media": last, "position": last["position"] or 0, "state": "resume"}
        idx = items.index(last)
        # next episode that is a different (season, episode) than the one just finished
        for nxt in items[idx + 1:]:
            if (nxt["season"], nxt["episode"]) != (last["season"], last["episode"]) and not nxt["completed"]:
                return {"media": nxt, "position": nxt["position"] or 0, "state": "next"}
        return {"media": last, "position": 0, "state": "done"}

    def continue_watching(self, limit: int = 20) -> list[dict]:
        rows = self.q(
            f"""SELECT m.title_id, MAX(p.updated_at) AS last
               FROM progress p JOIN media m ON m.id = p.media_id
               WHERE m.hidden = 0 AND (p.completed = 1 OR p.position >= {MIN_STARTED})
               GROUP BY m.title_id ORDER BY last DESC LIMIT ?""",
            (limit * 2,),
        )
        out = []
        for row in rows:
            point = self.resume_point(row["title_id"])
            if not point or point["state"] == "done":
                continue
            title = self.get_title(row["title_id"])
            out.append({"title": title, **point})
            if len(out) >= limit:
                break
        return out

    def neighbours(self, media_id: int) -> tuple[dict | None, dict | None]:
        media = self.get_media(media_id)
        if not media:
            return None, None
        items = self.title_media(media["title_id"])
        title = self.get_title(media["title_id"])
        key = self.episode_key(media, title["kind"])
        keys = [self.episode_key(m, title["kind"]) for m in items]
        if key not in keys:
            return None, None
        i = keys.index(key)
        return (items[i - 1] if i > 0 else None), (items[i + 1] if i + 1 < len(items) else None)

    def find_title_by_tmdb(self, kind: str, tmdb_id: int, exclude: int) -> dict | None:
        return self.q1("SELECT * FROM titles WHERE kind = ? AND tmdb_id = ? AND id != ? ORDER BY id",
                       (kind, tmdb_id, exclude))


def quality_of(m: dict) -> int:
    """Vertical resolution class (1080, 720, ...) from Telegram metadata or the caption/file name."""
    from .parser import quality_from_text

    h, w = m.get("height") or 0, m.get("width") or 0
    meta = max(h, round(w * 9 / 16)) if w > h else h  # 1920x800 (cinemascope) is still "1080p"
    return max(meta, quality_from_text(m.get("caption"), m.get("file_name")))
