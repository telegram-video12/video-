"""SQLite storage: chats, titles (series/movies), media files and watch progress."""
from __future__ import annotations

import json
import logging
import re
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

CREATE TABLE IF NOT EXISTS episode_info (
    title_id INTEGER NOT NULL,
    season   INTEGER NOT NULL,
    episode  INTEGER NOT NULL,
    name     TEXT,
    overview TEXT,
    still    TEXT,
    PRIMARY KEY (title_id, season, episode)
);

CREATE TABLE IF NOT EXISTS episode_seasons (
    title_id   INTEGER NOT NULL,
    season     INTEGER NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (title_id, season)
);

CREATE TABLE IF NOT EXISTS watchlist (
    title_id INTEGER PRIMARY KEY,
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tmdb_extras (  -- the title page's trailer / recommendations / next episode (kept a day)
    title_id   INTEGER PRIMARY KEY,
    data       TEXT NOT NULL,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS prefs (
    key   TEXT PRIMARY KEY,
    value TEXT
);

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

HEBREW = re.compile(r"[\u0590-\u05ff]")
COMPLETE_RATIO = 0.92  # watched this much -> episode counts as finished
MIN_STARTED = 10  # seconds; peeking at an episode for less than this does not count as watching it


def mark_sequels(rows: list[dict]) -> None:
    """A movie you have not watched gets "sequel" when you watched an EARLIER movie of the same movie series
    (TMDB collection), e.g. you saw "אבא גנוב" and "אבא גנוב 2" is in the library."""
    seen: dict[int, int] = {}  # collection -> the earliest year you watched in it
    for t in rows:
        if t["kind"] == "movie" and t.get("collection_id") and t["last_watched"]:
            y = t["year"] or 0
            seen[t["collection_id"]] = min(seen.get(t["collection_id"], y), y)
    now = time.time()
    this_year = time.gmtime(now).tm_year
    for t in rows:
        c = t.get("collection_id")
        # not out yet by TMDB (a trailer or a fake upload "of" a movie that comes out in 2028): no badge
        unreleased = (t.get("avail_at") or 0) > now or (t["year"] or 0) > this_year
        t["sequel"] = int(bool(t["kind"] == "movie" and c and c in seen and not t["last_watched"]
                               and not t["watched_count"] and (t["year"] or 9999) > seen[c] and not unreleased))


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.version = 0  # bumped on every write; used to cache the library listing
        self.soft_version = 0  # background poster/overview updates: refresh the cache at most every 20s
        self._titles_cache: tuple[int, int, float, list[dict]] | None = None
        self._build_lock = threading.Lock()
        self._refreshing = False
        self.soft_refresh_seconds = 10  # 60 while TMDB updates the whole library in the background
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL, and much faster saving on Windows
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(chats)")}
        if "default_season" not in cols:
            self.conn.execute("ALTER TABLE chats ADD COLUMN default_season INTEGER")
        if "season_label" not in cols:
            self.conn.execute("ALTER TABLE chats ADD COLUMN season_label TEXT")
        if "branding" not in cols:
            self.conn.execute("ALTER TABLE chats ADD COLUMN branding TEXT")
        if "origin" not in cols:  # 'il' / 'intl' / NULL (automatic): where this group's shows come from
            self.conn.execute("ALTER TABLE chats ADD COLUMN origin TEXT")
        tcols = {r[1] for r in self.conn.execute("PRAGMA table_info(titles)")}
        if "name_key" not in tcols:  # normalize(name): lets uploads find a title by its TMDB name too
            from .parser import normalize

            self.conn.execute("ALTER TABLE titles ADD COLUMN name_key TEXT")
            rows = self.conn.execute("SELECT id, name FROM titles").fetchall()
            self.conn.executemany("UPDATE titles SET name_key = ? WHERE id = ?",
                                  [(normalize(r[1] or ""), r[0]) for r in rows])
        for col, typ in (("genres", "TEXT"), ("rating", "REAL"), ("runtime", "INTEGER"), ("cast_json", "TEXT"),
                         ("original_name", "TEXT"), ("tmdb_details", "INTEGER NOT NULL DEFAULT 0"),
                         ("intro_start", "REAL"), ("intro_end", "REAL"),  # where the opening song is (per series)
                         ("last_year", "INTEGER"),  # series: the year of its latest episode (TMDB)
                         ("name_manual", "INTEGER NOT NULL DEFAULT 0"),  # you renamed it: TMDB keeps out
                         ("hidden_title", "INTEGER NOT NULL DEFAULT 0"),  # you hid it from the library
                         ("origin", "TEXT"),  # 'il' Israeli / 'intl' from abroad / NULL unknown
                         ("collection_id", "INTEGER"),
                         # when TMDB says it came out to watch (movie: digital release; series: its latest aired
                         # episode, which one, and whether it ended a season). NULL = not asked yet, 0 = unknown
                         ("avail_at", "REAL"), ("last_ep_season", "INTEGER"), ("last_ep_number", "INTEGER"),
                         ("last_ep_finale", "INTEGER"),
                         ("made_in_il", "INTEGER")):  # Israeli by TMDB (country / Hebrew); NULL = not asked yet  # movie: its TMDB movie series (0 = none, NULL = not known yet)  # 'il' Israeli / 'intl' from abroad / NULL unknown
            if col not in tcols:  # TMDB details (genres, rating, length, cast)
                self.conn.execute(f"ALTER TABLE titles ADD COLUMN {col} {typ}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS titles_name_key ON titles(kind, name_key)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS titles_norm ON titles(norm_name)")
        pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(progress)")}
        if "history_hidden" not in pcols:  # removed from "history" by you (the progress itself stays)
            self.conn.execute("ALTER TABLE progress ADD COLUMN history_hidden INTEGER NOT NULL DEFAULT 0")
        mcols = {r[1] for r in self.conn.execute("PRAGMA table_info(media)")}
        if "auto_norm" not in mcols:  # the name key the automatic parser gave this video
            self.conn.execute("ALTER TABLE media ADD COLUMN auto_norm TEXT")
        if "manual" not in mcols:  # set when you fix an episode/movie by hand; automatic regrouping skips it
            self.conn.execute("ALTER TABLE media ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
        if "found_at" not in mcols:  # when the SITE got this video (a scan found it), not when Telegram posted it
            self.conn.execute("ALTER TABLE media ADD COLUMN found_at REAL")
            self.conn.execute("UPDATE media SET found_at = added_at")  # what is already here is not "new"
        self.conn.execute("CREATE INDEX IF NOT EXISTS media_found ON media(found_at)")

    # ---------------------------------------------------------------- helpers
    def q(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def q1(self, sql: str, args: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple | list = (), soft: bool = False) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            if soft:
                self.soft_version += 1
            else:
                self.version += 1
            return cur.lastrowid

    # ------------------------------------------------------------------ prefs
    def get_pref(self, key: str, default: str = "") -> str:
        row = self.q1("SELECT value FROM prefs WHERE key = ?", (key,))
        return row["value"] if row and row["value"] is not None else default

    def set_pref(self, key: str, value: str) -> None:
        self.x("INSERT INTO prefs (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, value))

    def strip_phrases(self) -> list[str]:
        """Words/phrases you asked to remove from every name (Settings)."""
        return [p.strip() for p in self.get_pref("strip_phrases").splitlines() if p.strip()]

    # ------------------------------------------------------------------ chats
    def upsert_chat(self, chat_id: int, title: str, enabled: bool,
                    default_season: int | None = None, season_label: str | None = None, origin: str | None = None) -> None:
        self.x(
            """INSERT INTO chats (id, title, enabled, default_season, season_label, origin) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title = excluded.title, enabled = excluded.enabled,
                 default_season = excluded.default_season, season_label = excluded.season_label,
                 origin = excluded.origin""",
            (chat_id, title, int(enabled), default_season, season_label or None, origin),
        )

    def set_chat_branding(self, chat_id: int, branding) -> None:
        self.x("UPDATE chats SET branding = ? WHERE id = ?", (branding, chat_id))

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
    @staticmethod
    def find_title(conn, kind: str, norm_name: str, year: int | None) -> int | None:
        """A title for this name: same key, or a title whose (TMDB) display name gives the same key
        and whose year does not contradict (so a remake is not merged into the original)."""
        row = conn.execute("SELECT id, year FROM titles WHERE kind = ? AND norm_name = ?", (kind, norm_name)).fetchone()
        if row and (kind != "movie" or not year or not row[1] or abs(year - row[1]) <= 1):
            return row[0]
        if row:  # same name, years far apart: a remake is its own movie
            alt = conn.execute("SELECT id FROM titles WHERE kind = ? AND norm_name = ?",
                               (kind, f"{norm_name}#{year}")).fetchone()
            return alt[0] if alt else None
        for tid, y in conn.execute("SELECT id, year FROM titles WHERE kind = ? AND name_key = ? "
                                   "ORDER BY tmdb_id IS NULL, id", (kind, norm_name)):
            if not year or not y or abs(year - y) <= 1:
                return tid
        return None

    @classmethod
    def resolve_title(cls, conn, kind: str, norm_name: str, year: int | None, origin: str | None) -> tuple[int | None, str]:
        """The title for this name and origin, or the key a new one gets. An Israeli and a foreign show with the
        very same name ("הישרדות") are two titles: the second one gets the key "<name>@il" / "<name>@intl"."""
        tid = cls.find_title(conn, kind, norm_name, year)
        if tid is not None and origin:
            row = conn.execute("SELECT origin FROM titles WHERE id = ?", (tid,)).fetchone()
            if row and row[0] and row[0] != origin:
                tid = None
                alt = conn.execute("SELECT id FROM titles WHERE kind = ? AND norm_name = ?",
                                   (kind, f"{norm_name}@{origin}")).fetchone()
                if alt:
                    return alt[0], ""
                return None, f"{norm_name}@{origin}"
            if row and not row[0]:  # the first known origin is taken by the title
                conn.execute("UPDATE titles SET origin = ? WHERE id = ?", (origin, tid))
        if tid is None and origin:
            alt = conn.execute("SELECT id FROM titles WHERE kind = ? AND norm_name = ?",
                               (kind, f"{norm_name}@{origin}")).fetchone()
            if alt:
                return alt[0], ""
        return tid, cls.free_key(conn, kind, norm_name, year)

    def get_or_create_title(self, kind: str, name: str, norm_name: str, year: int | None, soft: bool = False,
                            origin: str | None = None) -> int:
        from .parser import normalize

        with self._lock:
            tid, key = self.resolve_title(self.conn, kind, norm_name, year, origin)
        if tid:
            return tid
        return self.x(
            "INSERT INTO titles (kind, name, norm_name, name_key, year, created_at, origin) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, name, key, normalize(name), year, time.time(), origin),
            soft=soft,
        )

    @staticmethod
    def free_key(conn, kind: str, norm_name: str, year: int | None) -> str:
        """The key for a NEW title: a remake whose name is taken gets the year added."""
        taken = conn.execute("SELECT 1 FROM titles WHERE kind = ? AND norm_name = ?", (kind, norm_name)).fetchone()
        return f"{norm_name}#{year}" if taken and year else norm_name

    def merge_by_name(self, name_key: str | None = None, soft: bool = False) -> int:
        """Merge a title without TMDB info into a TMDB title whose name gives the same key
        ("ספיידרמן אין דרך הבייתה" -> "ספיידרמן: אין דרך הביתה")."""
        pairs = self.q(
            """SELECT b.id AS src, MIN(a.id) AS dst FROM titles b
               JOIN titles a ON a.kind = b.kind AND a.name_key = b.norm_name AND a.id != b.id
               WHERE b.tmdb_id IS NULL AND a.tmdb_id IS NOT NULL
                 AND (a.year IS NULL OR b.year IS NULL OR ABS(a.year - b.year) <= 1)
                 AND (? IS NULL OR b.norm_name = ?)
               GROUP BY b.id""",
            (name_key, name_key),
        )
        for p in pairs:
            if self.get_title(p["src"]) and self.get_title(p["dst"]):
                self.merge_titles(p["src"], p["dst"], soft=soft)
        return len(pairs)

    def update_title(self, title_id: int, soft: bool = False, **fields: Any) -> None:
        allowed = {"kind", "name", "norm_name", "year", "overview", "poster", "backdrop", "tmdb_id", "tmdb_checked",
                   "genres", "rating", "runtime", "cast_json", "original_name", "tmdb_details",
                   "intro_start", "intro_end", "last_year", "name_manual", "hidden_title", "origin", "collection_id",
                   "avail_at", "last_ep_season", "last_ep_number", "last_ep_finale",
                   "made_in_il"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        if "name" in fields:
            from .parser import normalize

            fields["name_key"] = normalize(fields["name"] or "")
        if "tmdb_id" in fields:  # linked to another show: its episode names no longer apply
            cur = self.q1("SELECT tmdb_id FROM titles WHERE id = ?", (title_id,))
            if cur and cur["tmdb_id"] != fields["tmdb_id"]:
                self.clear_episode_info(title_id)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.x(f"UPDATE titles SET {sets} WHERE id = ?", (*fields.values(), title_id), soft=soft)

    def clear_episode_info(self, title_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM episode_info WHERE title_id = ?", (title_id,))
            self.conn.execute("DELETE FROM episode_seasons WHERE title_id = ?", (title_id,))
            self.conn.commit()

    def title_channels(self, title_id: int) -> list[dict]:
        """The groups this title's videos came from, with how many each."""
        return self.q("""SELECT m.chat_id, COALESCE(c.title, CAST(m.chat_id AS TEXT)) AS title, c.origin, COUNT(*) AS n
                         FROM media m LEFT JOIN chats c ON c.id = m.chat_id
                         WHERE m.title_id = ? GROUP BY m.chat_id ORDER BY n DESC""", (title_id,))

    def split_by_channel(self, title_id: int, chat_id: int) -> int:
        """Move one group's videos of this title into a separate title of the same name (a different show that
        shares the name, e.g. the American "הישרדות" next to the Israeli one). Returns the new title's id."""
        t = self.get_title(title_id)
        chat = self.get_chat(chat_id) or {}
        origin = chat.get("origin")
        base = (t["norm_name"] or "").split("@")[0]
        with self._lock:
            conn = self.conn
            key = f"{base}@{origin}" if origin and origin != t.get("origin") else f"{base}@ch{abs(chat_id)}"
            row = conn.execute("SELECT id FROM titles WHERE kind = ? AND norm_name = ?", (t["kind"], key)).fetchone()
            if row:
                new_id = row[0]
            else:
                new_id = conn.execute(
                    "INSERT INTO titles (kind, name, norm_name, name_key, year, created_at, origin) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (t["kind"], t["name"], key, t.get("name_key") or base, None, time.time(), origin)).lastrowid
            # manual = 1: your split is never undone by the automatic re-filing
            conn.execute("UPDATE media SET title_id = ?, manual = 1 WHERE title_id = ? AND chat_id = ?",
                         (new_id, title_id, chat_id))
            conn.execute("DELETE FROM titles WHERE id NOT IN (SELECT DISTINCT title_id FROM media)")
            conn.commit()
            self.version += 1
        return new_id

    def merge_titles(self, source_id: int, target_id: int, soft: bool = False) -> None:
        """soft: a background merge (TMDB) - the library list refreshes within 20s instead of at once,
        so thousands of background merges do not rebuild the list on every page load."""
        with self._lock:
            self.conn.execute("UPDATE media SET title_id = ? WHERE title_id = ?", (target_id, source_id))
            self.conn.execute("UPDATE OR IGNORE watchlist SET title_id = ? WHERE title_id = ?", (target_id, source_id))
            self.conn.execute("DELETE FROM watchlist WHERE title_id = ?", (source_id,))
            self.conn.execute("DELETE FROM titles WHERE id = ?", (source_id,))
            self.conn.commit()
            if soft:
                self.soft_version += 1
            else:
                self.version += 1

    def orphan_media(self) -> list[dict]:
        """Videos whose title row is missing (should not happen; repaired at start-up)."""
        return self.q("SELECT * FROM media WHERE title_id NOT IN (SELECT id FROM titles)")

    def delete_empty_titles(self) -> None:
        self.x("DELETE FROM titles WHERE id NOT IN (SELECT DISTINCT title_id FROM media)")

    def titles_needing_tmdb(self) -> list[dict]:
        """Titles still without TMDB info; ones you watch and newest uploads first."""
        return self.q(
            """SELECT t.* FROM titles t
               LEFT JOIN (SELECT m.title_id, MAX(p.updated_at) AS w, MAX(m.added_at) AS a
                          FROM media m LEFT JOIN progress p ON p.media_id = m.id GROUP BY m.title_id) x
                 ON x.title_id = t.id
               WHERE t.tmdb_checked = 0 OR (t.tmdb_id IS NOT NULL AND t.tmdb_details = 0)
               ORDER BY x.w IS NULL, x.w DESC, x.a DESC"""
        )

    def library_titles(self) -> list[dict]:
        """Cached list of everything in the library, recomputed after something changed.
        Your own changes (a new version) are waited for; background updates (TMDB posters / dates, a scan)
        only refresh it in the background: until then the previous list is served at once, so no page waits."""
        c = self._titles_cache
        if c and c[0] == self.version:
            if c[1] != self.soft_version and time.time() - c[2] > self.soft_refresh_seconds:
                self._refresh_titles_soon()
            return c[3]
        with self._build_lock:  # one build at a time; who waited gets the list the other one built
            c = self._titles_cache
            if not c or c[0] != self.version:
                self._build_titles()
            return self._titles_cache[3]

    def _refresh_titles_soon(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True

        def run() -> None:
            try:
                with self._build_lock:
                    c = self._titles_cache
                    if c and (c[0], c[1]) != (self.version, self.soft_version):
                        self._build_titles()
            except Exception:  # noqa: BLE001 - the next request tries again
                logging.getLogger(__name__).exception("refreshing the library list failed")
            finally:
                self._refreshing = False
        threading.Thread(target=run, daemon=True).start()

    def _build_titles(self) -> None:
        from .parser import normalize, skeleton

        version, soft = self.version, self.soft_version  # changes made while building make it stale again

        rows = self.list_titles()
        fresh = self.new_episodes()
        mark_sequels(rows)
        has_latest = self.have_latest_episode(time.time() - 61 * 86400)
        for t in rows:
            if t["kind"] == "series" and t["id"] not in has_latest:
                t["avail_at"] = None  # the newest episode TMDB knows is not on the site yet: nothing new to watch
            t["new_count"], t["new_at"] = fresh.get(t["id"], (0, None))  # search keys and parsed genres, computed once per change
            t["_key"] = normalize(t["name"] or "")
            t["_okey"] = normalize(t["original_name"] or "")
            t["_skel"] = skeleton(t["_key"])
            try:
                t["genres"] = json.loads(t["genres"] or "[]")
            except ValueError:
                t["genres"] = []
            # "ישראלי" as a genre: by TMDB (made in Israel / Hebrew) when it knows; otherwise an Israeli
            # group/caption or a Hebrew original name. (A caption saying "yes"/"HOT" = only SHOWN in Israel.)
            il = t["made_in_il"] if t.get("made_in_il") is not None else (
                t.get("origin") == "il" or HEBREW.search(t["original_name"] or ""))
            if il and "ישראלי" not in t["genres"]:
                t["genres"].append("ישראלי")
        self._titles_cache = (version, soft, time.time(), rows)

    def have_latest_episode(self, since: float) -> set[int]:
        """Series that aired an episode lately (by TMDB) AND have that episode (or a later one) on the site."""
        return {r["title_id"] for r in self.q(
            """SELECT DISTINCT m.title_id FROM titles t JOIN media m ON m.title_id = t.id AND m.hidden = 0
               WHERE t.kind = 'series' AND t.avail_at > ? AND t.last_ep_number IS NOT NULL
                 AND (COALESCE(m.season, 1) > COALESCE(t.last_ep_season, 1)
                      OR (COALESCE(m.season, 1) = COALESCE(t.last_ep_season, 1) AND m.episode >= t.last_ep_number))""",
            (since,))}

    def new_episodes(self) -> dict[int, tuple[int, float]]:
        """Series you watch that got episodes AFTER you last watched them (and you have not seen yet):
        {title_id: (how many new episodes, when the newest arrived)}."""
        rows = self.q(
            f"""WITH w AS (SELECT m.title_id, MAX(p.updated_at) AS last FROM progress p JOIN media m ON m.id = p.media_id
                           WHERE p.completed = 1 OR p.position >= {MIN_STARTED} GROUP BY m.title_id),
                     done AS (SELECT DISTINCT m.title_id, {EP_KEY} AS k FROM progress p JOIN media m ON m.id = p.media_id
                              JOIN titles t ON t.id = m.title_id WHERE p.completed = 1)
                SELECT m.title_id, COUNT(DISTINCT {EP_KEY}) AS n, MAX(m.added_at) AS newest
                FROM media m JOIN titles t ON t.id = m.title_id AND t.kind = 'series'
                JOIN w ON w.title_id = m.title_id
                WHERE m.hidden = 0 AND m.added_at > w.last
                  AND NOT EXISTS (SELECT 1 FROM done d WHERE d.title_id = m.title_id AND d.k = {EP_KEY})
                GROUP BY m.title_id"""
        )
        return {r["title_id"]: (r["n"], r["newest"]) for r in rows}

    def last_watched_at(self, title_id: int) -> float | None:
        r = self.q1(f"""SELECT MAX(p.updated_at) AS last FROM progress p JOIN media m ON m.id = p.media_id
                        WHERE m.title_id = ? AND (p.completed = 1 OR p.position >= {MIN_STARTED})""", (title_id,))
        return r["last"] if r else None

    def list_titles(self) -> list[dict]:
        return self.q(
            f"""
            SELECT t.id, t.kind, t.name, t.year, t.last_year, t.origin, t.collection_id, t.avail_at, t.last_ep_finale, t.made_in_il, t.poster, t.backdrop, t.overview, t.genres, t.rating,
                   t.original_name,
                   COUNT(DISTINCT {EP_KEY}) AS media_count,
                   COUNT(DISTINCT m.season) AS season_count,
                   MAX(m.added_at) AS last_added,
                   MAX(m.id) AS last_in,
                   MIN(m.id) AS first_media_id,
                   COUNT(DISTINCT CASE WHEN p.completed = 1 THEN {EP_KEY} END) AS watched_count,
                   MAX(CASE WHEN p.completed = 1 OR p.position >= {MIN_STARTED} THEN p.updated_at END) AS last_watched,
                   MAX(m.duration) AS duration
            FROM titles t
            JOIN media m ON m.title_id = t.id AND m.hidden = 0
            LEFT JOIN progress p ON p.media_id = m.id
            WHERE t.hidden_title = 0
            GROUP BY t.id
            ORDER BY last_in DESC  -- newest in YOUR library first (not by the date it was posted on Telegram)
            """
        )

    def get_title(self, title_id: int) -> dict | None:
        return self.q1("SELECT * FROM titles WHERE id = ?", (title_id,))

    # ------------------------------------------------------------------ media
    def has_media(self, chat_id: int, msg_id: int) -> bool:
        return self.q1("SELECT 1 AS x FROM media WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id)) is not None

    def add_media(self, _soft: bool = False, **m: Any) -> int | None:
        """Insert a media row; existing (chat_id, msg_id) rows are left untouched so manual edits survive.
        _soft: during a scan - the library list refreshes every few seconds instead of after every video."""
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO media
                   (chat_id, msg_id, title_id, season, episode, file_name, caption, size, mime,
                    duration, width, height, added_at, auto_norm, found_at)
                   VALUES (:chat_id, :msg_id, :title_id, :season, :episode, :file_name, :caption, :size,
                           :mime, :duration, :width, :height, :added_at, :auto_norm, :found_at)""",
                {"auto_norm": None, "found_at": time.time(), **m},
            )
            self.conn.commit()
            if _soft:
                self.soft_version += 1
            else:
                self.version += 1
            return cur.lastrowid if cur.rowcount else None

    def get_media(self, media_id: int) -> dict | None:
        return self.q1(
            """SELECT m.*, p.position, p.completed, p.updated_at AS watched_at
               FROM media m LEFT JOIN progress p ON p.media_id = m.id WHERE m.id = ?""",
            (media_id,),
        )

    def update_media(self, media_id: int, **fields: Any) -> None:
        allowed = {"title_id", "season", "episode", "hidden", "manual"}
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
                 updated_at = excluded.updated_at, history_hidden = 0""",
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
        else:  # keep the row (as "not watched", now) so other devices learn about it when syncing
            self.x("UPDATE progress SET completed = 0, position = 0, updated_at = ? WHERE media_id = ?",
                   (time.time(), media_id))

    def hide_from_continue(self, title_id: int, hidden: bool) -> None:
        try:
            data = json.loads(self.get_pref("cont_hidden") or "{}")
        except ValueError:
            data = {}
        if hidden:
            data[str(title_id)] = time.time()
        else:
            data.pop(str(title_id), None)
        self.set_pref("cont_hidden", json.dumps(data))
        self.soft_version += 1

    # ------------------------------------------------------- episode names (TMDB)
    def episode_info(self, title_id: int) -> dict[tuple[int, int], dict]:
        return {(r["season"], r["episode"]): r for r in
                self.q("SELECT season, episode, name, overview, still FROM episode_info WHERE title_id = ?", (title_id,))}

    def seasons_to_fetch(self, title_id: int, max_age: float = 7 * 86400) -> list[int]:
        """Seasons of this series (as you have them) whose episode names were not fetched lately."""
        rows = self.q("""SELECT DISTINCT COALESCE(m.season, 1) AS s FROM media m
                         LEFT JOIN episode_seasons e ON e.title_id = m.title_id AND e.season = COALESCE(m.season, 1)
                         WHERE m.title_id = ? AND m.episode IS NOT NULL AND m.hidden = 0
                           AND (e.fetched_at IS NULL OR e.fetched_at < ?)""", (title_id, time.time() - max_age))
        return [r["s"] for r in rows]

    def save_episode_info(self, title_id: int, season: int, episodes: list[dict]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO episode_info (title_id, season, episode, name, overview, still) VALUES (?, ?, ?, ?, ?, ?)",
                [(title_id, season, e["episode"], e.get("name"), e.get("overview"), e.get("still")) for e in episodes])
            self.conn.execute("INSERT OR REPLACE INTO episode_seasons (title_id, season, fetched_at) VALUES (?, ?, ?)",
                              (title_id, season, time.time()))
            self.conn.commit()

    # ------------------------------------------------------------ my list
    def watchlist_ids(self) -> list[int]:
        return [r["title_id"] for r in self.q("SELECT title_id FROM watchlist ORDER BY added_at DESC")]

    def set_watchlist(self, title_id: int, on: bool) -> None:
        if on:
            self.x("INSERT OR IGNORE INTO watchlist (title_id, added_at) VALUES (?, ?)", (title_id, time.time()), soft=True)
        else:
            self.x("DELETE FROM watchlist WHERE title_id = ?", (title_id,), soft=True)

    def history(self, limit: int = 300) -> list[dict]:
        """What you actually watched (not episodes only ticked as watched), newest first."""
        return self.q(
            f"""SELECT p.media_id, p.position, p.duration, p.completed, p.updated_at,
                       m.season, m.episode, m.file_name, m.caption, m.duration AS media_duration,
                       t.id AS title_id, t.name, t.kind, t.poster, t.backdrop
                FROM progress p JOIN media m ON m.id = p.media_id JOIN titles t ON t.id = m.title_id
                WHERE p.position >= {MIN_STARTED} AND p.history_hidden = 0
                ORDER BY p.updated_at DESC LIMIT ?""",
            (limit,),
        )

    def hide_history(self, media_ids: list[int]) -> None:
        with self._lock:
            self.conn.executemany("UPDATE progress SET history_hidden = 1 WHERE media_id = ?", [(i,) for i in media_ids])
            self.conn.commit()

    def titles_by_caption(self, text: str, limit: int = 30) -> list[int]:
        """Where did videos whose Telegram text/file name contains this go? (they may sit under another name)"""
        like = f"%{text}%"
        rows = self.q("""SELECT title_id, COUNT(*) AS n FROM media
                         WHERE hidden = 0 AND (caption LIKE ? OR file_name LIKE ?)
                         GROUP BY title_id ORDER BY n DESC LIMIT ?""", (like, like, limit))
        return [r["title_id"] for r in rows]

    def most_watched(self, limit: int = 10) -> list[int]:
        """Titles you spent the most time watching (the place reached in each video counts)."""
        rows = self.q(f"""SELECT m.title_id, SUM(p.position) AS s FROM progress p JOIN media m ON m.id = p.media_id
                          WHERE p.position >= {MIN_STARTED} GROUP BY m.title_id ORDER BY s DESC LIMIT ?""", (limit * 2,))
        return [r["title_id"] for r in rows]

    def watch_stats(self) -> dict:
        """Roughly how much you watched: the place you reached in each video counts as time watched."""
        now = time.time()
        rows = self.q(
            f"""SELECT p.position, p.completed, p.updated_at, t.kind, t.genres, t.id AS title_id
                FROM progress p JOIN media m ON m.id = p.media_id JOIN titles t ON t.id = m.title_id
                WHERE p.position >= {MIN_STARTED}"""
        )

        def total(days: float | None) -> dict:
            sel = [r for r in rows if days is None or r["updated_at"] >= now - days * 86400]
            return {"seconds": sum(r["position"] for r in sel),
                    "episodes": sum(1 for r in sel if r["completed"] and r["kind"] == "series"),
                    "movies": sum(1 for r in sel if r["completed"] and r["kind"] == "movie"),
                    "titles": len({r["title_id"] for r in sel})}

        day0 = time.localtime(now)
        midnight = time.mktime((day0.tm_year, day0.tm_mon, day0.tm_mday, 0, 0, 0, 0, 0, -1))
        days = []
        for i in range(13, -1, -1):  # the last 14 days, oldest first
            lo = midnight - i * 86400
            days.append({"day": lo, "seconds": sum(r["position"] for r in rows if lo <= r["updated_at"] < lo + 86400)})
        genres: dict[str, float] = {}
        for r in rows:
            try:
                for g in json.loads(r["genres"] or "[]"):
                    genres[g] = genres.get(g, 0) + r["position"]
            except ValueError:
                pass
        top = sorted(genres.items(), key=lambda x: -x[1])[:6]
        return {"week": total(7), "month": total(30), "all": total(None), "days": days,
                "genres": [{"name": g, "seconds": sec} for g, sec in top]}

    # ---------------------------------------------------------- device sync
    def export_progress(self) -> list[dict]:
        """Watch progress keyed by the Telegram message (the same on every device)."""
        return self.q("""SELECT m.chat_id AS c, m.msg_id AS m, p.position AS pos, p.duration AS dur,
                                p.completed AS done, p.updated_at AS t
                         FROM progress p JOIN media m ON m.id = p.media_id""")

    def import_progress(self, rows: list[dict]) -> int:
        """Merge another device's progress: for each video the most recent change wins."""
        changed = 0
        with self._lock:
            conn = self.conn
            for r in rows:
                try:
                    c, m, t = int(r["c"]), int(r["m"]), float(r["t"])
                    pos, done = float(r.get("pos") or 0), int(bool(r.get("done")))
                    dur = float(r["dur"]) if r.get("dur") else None
                except (KeyError, TypeError, ValueError):
                    continue
                media = conn.execute("SELECT id FROM media WHERE chat_id = ? AND msg_id = ?", (c, m)).fetchone()
                if not media:
                    continue  # that video was not scanned on this device (yet)
                mine = conn.execute("SELECT updated_at FROM progress WHERE media_id = ?", (media[0],)).fetchone()
                if mine and mine[0] >= t:
                    continue
                conn.execute(
                    """INSERT INTO progress (media_id, position, duration, completed, updated_at) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(media_id) DO UPDATE SET position = excluded.position, duration = excluded.duration,
                         completed = excluded.completed, updated_at = excluded.updated_at""",
                    (media[0], pos, dur, done, t))
                changed += 1
            conn.commit()
            if changed:
                self.version += 1
        return changed

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
        try:  # titles you removed from "continue watching" (until you watch them again)
            hidden = json.loads(self.get_pref("cont_hidden") or "{}")
        except ValueError:
            hidden = {}
        out = []
        for row in rows:
            if hidden.get(str(row["title_id"]), 0) >= row["last"]:
                continue
            tt = self.get_title(row["title_id"])
            if tt and tt.get("hidden_title"):
                continue
            point = self.resume_point(row["title_id"])
            if not point or point["state"] == "done":
                continue
            title = self.get_title(row["title_id"])
            if not title:
                continue
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

    def find_title_by_name_year(self, kind: str, name: str, year: int | None, exclude: int) -> dict | None:
        return self.q1("SELECT * FROM titles WHERE kind = ? AND name = ? AND year IS ? AND tmdb_id IS NOT NULL "
                       "AND id != ? ORDER BY id", (kind, name, year, exclude))

    def find_title_by_tmdb(self, kind: str, tmdb_id: int, exclude: int) -> dict | None:
        return self.q1("SELECT * FROM titles WHERE kind = ? AND tmdb_id = ? AND id != ? ORDER BY id",
                       (kind, tmdb_id, exclude))


def quality_of(m: dict) -> int:
    """Vertical resolution class (1080, 720, ...) from Telegram metadata or the caption/file name."""
    from .parser import quality_from_text

    h, w = m.get("height") or 0, m.get("width") or 0
    meta = max(h, round(w * 9 / 16)) if w > h else h  # 1920x800 (cinemascope) is still "1080p"
    return max(meta, quality_from_text(m.get("caption"), m.get("file_name")))
