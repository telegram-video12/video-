"""Web server: private site with library, player, progress tracking and settings.

Built on Starlette (pure Python) so it installs on Android/Termux without compiling.
"""
from __future__ import annotations

import asyncio
import hashlib
import socket
import os
import unicodedata
import json
import hmac
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import crypto_boost
from .config import Settings
from .db import Database, quality_of
from .library import possible_duplicates
from .parser import fuzzy_contains, normalize, skeleton
from .telegram import Scanner, TelegramService
from .tmdb import TMDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
logging.getLogger("telethon").setLevel(logging.WARNING)  # keep the Termux screen readable
logging.getLogger("httpx").setLevel(logging.WARNING)     # its request log would print the TMDB key

STATIC = Path(__file__).parent / "static"
COOKIE = "tgv_session"
SESSION_DAYS = 365
PUBLIC_PATHS = {"/login", "/api/login", "/api/setup", "/api/setup-state", "/manifest.json",
                "/api/sync/exchange"}  # checks its own key (from the shared site password)
SYNC_EVERY_SECONDS = 5 * 60


class ApiError(Exception):
    def __init__(self, status: int, detail: str = "", headers: dict | None = None):
        self.status, self.detail, self.headers = status, detail or "שגיאה", headers


def ok(data=None, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True} if data is None else data, status_code=status)


async def body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def telegram_error_message(e: Exception) -> str:
    name = type(e).__name__
    return {
        "PhoneNumberInvalidError": "מספר הטלפון לא תקין. כתוב אותו עם קידומת, למשל +972501234567",
        "PhoneCodeInvalidError": "הקוד שגוי, נסה שוב",
        "PhoneCodeExpiredError": "הקוד פג תוקף. בקש קוד חדש",
        "PasswordHashInvalidError": "סיסמת האימות הדו-שלבי שגויה",
        "ApiIdInvalidError": "ה-API ID או ה-API Hash שגויים. בדוק אותם שוב ב-my.telegram.org",
        "FloodWaitError": f"טלגרם ביקשה להמתין {getattr(e, 'seconds', '')} שניות לפני ניסיון נוסף",
        "PhoneNumberBannedError": "המספר חסום בטלגרם",
        "SendCodeUnavailableError": "טלגרם כבר שלח קוד ולא מאפשר לשלוח שוב כרגע. הזן את הקוד האחרון שקיבלת, "
                                    "או סגור והפעל מחדש את האתר ונסה שוב בעוד כמה דקות",
    }.get(name, str(e) or name)


class AuthMiddleware:
    """Pure ASGI middleware (keeps video streaming efficient)."""

    def __init__(self, app, settings: Settings, valid_token, valid_cast=None):
        self.app, self.settings, self.valid_token, self.valid_cast = app, settings, valid_token, valid_cast

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        cast_ok = (path.startswith("/api/stream/") and self.valid_cast
                   and self.valid_cast(path, Request(scope).query_params.get("t")))
        client = (scope.get("client") or ("127.0.0.1", 0))[0]
        if os.getenv("LOCAL_ONLY") == "1" and not cast_ok and client not in ("127.0.0.1", "::1", "localhost"):
            # the phone: nobody else on the Wi-Fi may open the site, only a TV playing a video you sent it
            return await JSONResponse({"detail": "forbidden"}, status_code=403)(scope, receive, send)
        if cast_ok:
            return await self.app(scope, receive, send)
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await self.app(scope, receive, send)
        if True:  # note slow answers in the window, to find what makes a page slow
            started = time.monotonic()
            inner = send

            async def send(message):
                if message["type"] == "http.response.start":
                    took = time.monotonic() - started
                    if took > 1.5:
                        log.warning("slow: %s took %.1f s", path, took)
                await inner(message)
        request = Request(scope)
        if self.settings.needs_setup or not self.valid_token(request.cookies.get(COOKIE)):
            if path.startswith("/api/"):
                resp = JSONResponse({"detail": "login required"}, status_code=401)
            else:
                resp = RedirectResponse("/login", status_code=302)
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, tg: TelegramService | None = None) -> Starlette:
    settings = settings or Settings()
    db = Database(settings.db_path)
    tg = tg or TelegramService(str(settings.session_path), settings.api_id, settings.api_hash)
    scanner = Scanner(db, tg, TMDB(settings.tmdb_api_key) if settings.tmdb_api_key else None,
                      settings.min_duration_seconds)
    secret = settings.secret_key.encode()
    log.info("AES backend: %s", crypto_boost.install())

    # ------------------------------------------------------------ auth utils
    def make_token() -> str:
        exp = str(int(time.time()) + SESSION_DAYS * 86400)
        return f"{exp}.{hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()}"

    def cast_token(media_id: int) -> str:
        """A key for ONE video, valid 12 hours: the TV (Chromecast) has no login, it gets this instead."""
        exp = str(int(time.time()) + 12 * 3600)
        return f"{exp}.{hmac.new(secret, f'cast:{media_id}:{exp}'.encode(), hashlib.sha256).hexdigest()[:32]}"

    def valid_cast(path: str, token: str | None) -> bool:
        m = re.fullmatch(r"/api/stream/(\d+)", path)
        if not m or not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        good = hmac.new(secret, f"cast:{m.group(1)}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()

    def lan_ip() -> str | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                sk.connect(("10.255.255.255", 1))
                ip = sk.getsockname()[0]
            return None if ip.startswith("127.") else ip
        except OSError:
            return None

    def valid_token(token: str | None) -> bool:
        if not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        good = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()

    def logged_in(resp: JSONResponse) -> JSONResponse:
        resp.set_cookie(COOKIE, make_token(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")
        return resp

    attempts: list[float] = []

    async def setup_state(request: Request):
        return ok({"needs_setup": settings.needs_setup})

    async def setup(request: Request):
        if not settings.needs_setup:
            raise ApiError(403, "הסיסמה כבר הוגדרה")
        pw = str((await body(request)).get("password", ""))
        if len(pw) < 4:
            raise ApiError(400, "הסיסמה צריכה להיות לפחות 4 תווים")
        settings.set_password(pw)
        return logged_in(ok())

    async def login(request: Request):
        now = time.time()
        attempts[:] = [t for t in attempts if now - t < 60]
        if len(attempts) >= 10:
            raise ApiError(429, "יותר מדי ניסיונות, נסה שוב בעוד דקה")
        attempts.append(now)
        if settings.needs_setup:
            raise ApiError(400, "צריך קודם לבחור סיסמה")
        if not settings.check_password(str((await body(request)).get("password", ""))):
            raise ApiError(401, "סיסמה שגויה")
        return logged_in(ok())

    async def logout(request: Request):
        resp = ok()
        resp.delete_cookie(COOKIE)
        return resp

    async def change_password(request: Request):
        data = await body(request)
        if not settings.check_password(str(data.get("current", ""))):
            raise ApiError(401, "הסיסמה הנוכחית שגויה")
        new = str(data.get("new", ""))
        if len(new) < 4:
            raise ApiError(400, "הסיסמה צריכה להיות לפחות 4 תווים")
        settings.set_password(new)
        return ok()

    # ----------------------------------------------------------------- pages
    def page(name: str) -> HTMLResponse:
        """Serve a page whose CSS/JS links carry the files' version, so after an update the browser
        always loads the new design instead of an old copy from its cache."""
        html = (STATIC / name).read_text(encoding="utf-8")
        for asset in ("style.css", "app.js"):
            stamp = int((STATIC / asset).stat().st_mtime)
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={stamp}")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    async def login_page(request: Request):
        return page("login.html")

    async def index(request: Request):
        return page("index.html")

    async def manifest(request: Request):
        return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")

    # --------------------------------------------------------------- library
    def thumb_url(media_id: int | None) -> str | None:
        return f"/api/thumb/{media_id}" if media_id else None

    async def episode_names(t: dict | None, wait: float = 6) -> dict:
        """Episode names/summaries/pictures from TMDB for a series; fetched once a week per season, in the
        background if TMDB is slow (the page never waits more than a few seconds)."""
        if not t or t["kind"] != "series" or not t.get("tmdb_id") or not scanner.tmdb or t["id"] in names_off():
            return {}
        todo = db.seasons_to_fetch(t["id"])
        if todo:
            async def fetch():
                for s in todo:
                    try:
                        db.save_episode_info(t["id"], s, await scanner.tmdb.season(t["tmdb_id"], s))
                    except Exception as e:  # noqa: BLE001 - try again next time the page is opened
                        log.info("episode names for %s season %s not loaded: %s", t["name"], s, type(e).__name__)
                        return
            task = asyncio.ensure_future(fetch())
            try:
                await asyncio.wait_for(asyncio.shield(task), wait)
            except asyncio.TimeoutError:
                pass
        return db.episode_info(t["id"])

    def names_off() -> set[int]:
        """Series where you turned TMDB's episode names off (e.g. your channels number the seasons differently)."""
        try:
            return set(json.loads(db.get_pref("ep_names_off") or "[]"))
        except ValueError:
            return set()

    def with_names(m: dict | None, info: dict) -> dict | None:
        if m is None:
            return None
        e = info.get((m["season"] or 1, m["episode"])) if m.get("episode") is not None else None
        if e:
            m.update(ep_name=e["name"], ep_overview=e["overview"], ep_still=e["still"])
        return m

    def media_out(m: dict) -> dict:
        return {
            "id": m["id"],
            "title_id": m["title_id"],
            "season": m["season"],
            "episode": m["episode"],
            "file_name": m["file_name"],
            "caption": m["caption"],
            "size": m["size"],
            "duration": m["duration"] or m.get("watched_duration"),
            "height": m["height"],
            "hidden": bool(m["hidden"]),
            "position": m.get("position") or 0,
            "completed": bool(m.get("completed")),
            "watched_at": m.get("watched_at"),
            "thumb": thumb_url(m["id"]),
            "quality": m.get("quality") or quality_of(m),
            "added_at": m.get("added_at"),
            "versions": m.get("versions", 1),
            "ep_name": m.get("ep_name"),
            "ep_overview": m.get("ep_overview"),
            "ep_still": m.get("ep_still"),
        }

    async def library(request: Request):
        """Paged library: ?kind=all|series|movie&q=search&offset=0&limit=60"""
        qp = request.query_params
        kind = qp.get("kind", "all")
        raw_q = qp.get("q", "").strip()
        # "הישרדות עונה 8" / "הבורר ע2 פ5" / "Friends S05E03": search the name, remember the season
        want_season = None
        sm = re.search(r"(?<![א-ת])(?:עונה|ע['׳]?|season\s*|\bs)\s*(\d{1,2})", raw_q, re.I)
        if sm:
            want_season = int(sm.group(1))
            raw_q = (raw_q[:sm.start()] + " " + raw_q[sm.end():])
        raw_q = re.sub(r"(?<![א-ת])(?:פרק|פ['׳]?|episode\s*|\be)\s*\d{1,4}\b", " ", raw_q, flags=re.I).strip()
        q = normalize(raw_q)
        offset = max(0, int(qp.get("offset", 0) or 0))
        limit = min(200, max(1, int(qp.get("limit", 60) or 60)))
        items = db.library_titles()
        if q:  # Hebrew name or the original (TMDB) name: "Spider" finds "ספיידרמן"
            sk = skeleton(q)
            found = [t for t in items if q in t["_key"] or (t["_okey"] and q in t["_okey"]) or (len(sk) >= 3 and sk in t["_skel"])]
            if len(found) < 3 and len(q) >= 4:  # small typo: one letter wrong, missing or extra
                seen = {t["id"] for t in found}
                found += [t for t in items if t["id"] not in seen
                          and (fuzzy_contains(q, t["_key"]) or (t["_okey"] and fuzzy_contains(q, t["_okey"])))]
            items = found
        genres = [g for g in qp.get("genres", "").split(",") if g]
        if genres:  # any of the chosen genres
            items = [t for t in items if any(g in t["genres"] for g in genres)]
        state = qp.get("state", "all")
        if state != "all":
            def watch_state(t):
                if t["media_count"] and t["watched_count"] >= t["media_count"]:
                    return "done"
                return "watching" if t["last_watched"] else "new"
            items = [t for t in items if watch_state(t) == state]
        decade = qp.get("decade", "")
        if decade:
            lo, hi = {"2020": (2020, 9999), "2010": (2010, 2019), "2000": (2000, 2009),
                      "1990": (1990, 1999), "old": (0, 1989)}.get(decade, (0, 9999))
            items = [t for t in items if t["year"] and lo <= t["year"] <= hi]
        sort = qp.get("sort", "added")
        if sort == "watched":
            items = sorted(items, key=lambda t: t["last_watched"] or 0, reverse=True)
        elif sort == "name":
            items = sorted(items, key=lambda t: (not re.match(r"[א-ת]", t["name"] or ""), (t["name"] or "").lower()))
        elif sort == "year":
            items = sorted(items, key=lambda t: t["year"] or 0, reverse=True)
        elif sort == "release":  # newest first: a movie's year, a series' latest season; unknown years left out
            items = sorted((t for t in items if t["year"]),
                           key=lambda t: (t.get("last_year") or t["year"], t["year"]), reverse=True)
        elif sort == "rating":
            items = sorted(items, key=lambda t: t["rating"] or 0, reverse=True)
        if sort != "watched":  # titles with a real poster (TMDB) first, the ones without at the end, same order
            items = [t for t in items if t["poster"]] + [t for t in items if not t["poster"]]
        counts = {"all": len(items), "series": sum(1 for t in items if t["kind"] == "series")}
        counts["movie"] = counts["all"] - counts["series"]
        if kind in ("series", "movie"):
            items = [t for t in items if t["kind"] == kind]
        page = []
        for t in items[offset:offset + limit]:
            t = {k: v for k, v in t.items() if not k.startswith("_")}
            t["poster"] = t["poster"] or thumb_url(t["first_media_id"])
            page.append(t)
        out = {"titles": page, "total": len(items), "counts": counts, "offset": offset, "season": want_season}
        if q and offset == 0 and len(raw_q) >= 2 and qp.get("files") == "1":
            # also: videos whose TELEGRAM text has these words but that sit under another name on the site
            shown = {t["id"] for t in items}
            by_id = {t["id"]: t for t in db.library_titles()}
            extra = [by_id[i] for i in db.titles_by_caption(raw_q) if i in by_id and i not in shown
                     and (kind not in ("series", "movie") or by_id[i]["kind"] == kind)]
            out["by_file"] = [card(t) for t in extra[:30]]
        if offset == 0 and not q and not qp.get("nocont"):
            out["continue"] = [
                {
                    "title": {**{k: c["title"][k] for k in ("id", "name", "kind", "backdrop", "poster", "overview",
                                                             "year", "rating")},
                              "genres": json.loads(c["title"].get("genres") or "[]")},
                    "media": media_out(c["media"]),
                    "position": c["position"],
                    "state": c["state"],
                }
                for c in db.continue_watching()
            ]
        return ok(out)

    def card(t: dict) -> dict:
        t = {k: v for k, v in t.items() if not k.startswith("_")}
        t["poster"] = t["poster"] or thumb_url(t["first_media_id"])
        return t

    async def home_rows(request: Request):
        """Home page extras: new episodes of what you watch, and rows of the genres you watch most."""
        items = db.library_titles()
        fresh = sorted((t for t in items if t["new_count"]), key=lambda t: t["new_at"] or 0, reverse=True)
        # your taste: genres of what you watched (recent watching counts a little more)
        score: dict[str, float] = {}
        watched = sorted((t for t in items if t["last_watched"]), key=lambda t: t["last_watched"], reverse=True)
        for i, t in enumerate(watched[:300]):
            for g in t["genres"]:
                score[g] = score.get(g, 0) + 1 + 1 / (1 + i / 10)
        based_on_you = bool(score)
        if not score:  # nothing watched yet: the genres you have the most of
            for t in items:
                for g in t["genres"]:
                    score[g] = score.get(g, 0) + 1
        day = int(time.time() // 86400)  # the order changes once a day, so the rows feel fresh

        shown: set[int] = set()  # each title appears in one genre row only, so the rows show more variety

        def pick(g: str) -> list[dict]:
            cands = [t for t in items if g in t["genres"] and t["id"] not in shown
                     and not (t["media_count"] and t["watched_count"] >= t["media_count"])]
            cands.sort(key=lambda t: (not t["poster"],  # pictures first
                                      -(t["rating"] or 0) + ((t["id"] * 7919 + day) % 13) / 10))
            return [card(t) for t in cands[:20]]

        rows = []
        for g, _ in sorted(score.items(), key=lambda x: -x[1]):
            row = pick(g)
            if len(row) >= 4:
                rows.append({"genre": g, "titles": row})
                shown.update(t["id"] for t in row)
            if len(rows) >= 4:
                break
        return ok({"new": [card(t) for t in fresh[:20]], "genres": rows, "personal": based_on_you,
                   "watchlist": watchlist_cards()[:20], "featured": featured(items, day), "top": top_mine(items)})

    def top_mine(items: list[dict]) -> list[dict]:
        """"טופ 10 שלי": what you watched the most (shown once there are at least 3)."""
        by_id = {t["id"]: t for t in items}
        top = [card(by_id[i]) for i in db.most_watched(10) if i in by_id][:10]
        return top if len(top) >= 3 else []

    def featured(items: list[dict], day: int) -> list[dict]:
        """The big swipeable pictures at the top of the home page: new and highly rated titles that have a wide
        picture, not finished yet (not started first). A different pick every day."""
        import random

        this_year = time.localtime().tm_year
        scored = []
        for t in items:
            if not t["backdrop"] or (t["media_count"] and t["watched_count"] >= t["media_count"]):
                continue
            year = t.get("last_year") or t["year"] or 0
            rating = t["rating"] or 0
            recent = year >= this_year - 1
            if rating < 7 and not (recent and rating >= 6):
                continue
            score = rating + (1.5 if recent else 0.7 if year >= this_year - 3 else 0) - (1 if t["last_watched"] else 0)
            scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        pool = [t for _, t in scored[:24]]
        random.Random(day).shuffle(pool)
        out = []
        for t in pool[:10]:
            c = card(t)
            point = db.resume_point(t["id"])
            c["play_id"] = point["media"]["id"] if point else t["first_media_id"]
            c["recent"] = (t.get("last_year") or t["year"] or 0) >= this_year - 1
            out.append(c)
        return out

    def watchlist_cards() -> list[dict]:
        by_id = {t["id"]: t for t in db.library_titles()}
        return [card(by_id[i]) for i in db.watchlist_ids() if i in by_id]

    async def my_list(request: Request):
        """My list page: saved for later, what you watched, and how much."""
        history = [{
            "media_id": h["media_id"], "title_id": h["title_id"], "name": h["name"], "kind": h["kind"],
            "season": h["season"], "episode": h["episode"], "position": h["position"],
            "duration": h["duration"] or h["media_duration"], "completed": bool(h["completed"]),
            "at": h["updated_at"], "pic": h["backdrop"] or thumb_url(h["media_id"]),
        } for h in db.history()]
        return ok({"watchlist": watchlist_cards(), "history": history, "stats": db.watch_stats()})

    async def duplicates(request: Request):
        """Titles that look like the same movie/series with extra words, for you to decide."""
        pairs = possible_duplicates(db)
        return ok({"pairs": pairs[:300], "total": len(pairs)})

    async def duplicates_dismiss(request: Request):
        key = str((await body(request)).get("key") or "")
        if not re.fullmatch(r"\d+-\d+", key):
            raise ApiError(400, "bad key")
        try:
            done = json.loads(db.get_pref("dup_dismissed") or "[]")
        except ValueError:
            done = []
        if key not in done:
            done.append(key)
            db.set_pref("dup_dismissed", json.dumps(done))
        return ok()

    async def watchlist_set(request: Request):
        title_id = request.path_params["id"]
        if not db.get_title(title_id):
            raise ApiError(404, "לא נמצא")
        on = bool((await body(request)).get("on", True))
        db.set_watchlist(title_id, on)
        return ok({"on": on})

    async def hidden_titles(request: Request):
        """Series and movies you hid from the library (Settings lets you show them again)."""
        rows = db.q("SELECT id, kind, name, year, poster FROM titles WHERE hidden_title = 1 ORDER BY name")
        return ok({"titles": rows})

    async def continue_hide(request: Request):
        """Remove a title from "continue watching" (it comes back when you watch it again)."""
        data = await body(request)
        title_id = int(data.get("title_id") or 0)
        if not db.get_title(title_id):
            raise ApiError(404, "לא נמצא")
        db.hide_from_continue(title_id, bool(data.get("hidden", True)))
        return ok()

    async def history_hide(request: Request):
        ids = [int(i) for i in (await body(request)).get("ids", []) if str(i).isdigit()][:1000]
        db.hide_history(ids)
        return ok()

    async def random_pick(request: Request):
        """'Surprise me': something you have not started yet. ?kind=series|movie&not=<id>"""
        import random

        qp = request.query_params
        kind, skip = qp.get("kind", ""), qp.get("not", "")
        items = [t for t in db.library_titles()
                 if not t["watched_count"] and not t["last_watched"]
                 and (kind not in ("series", "movie") or t["kind"] == kind) and str(t["id"]) != skip]
        nice = [t for t in items if t["poster"] and t["overview"]]  # ones with a picture and a story first
        pool = nice or items
        if not pool:
            raise ApiError(404, "אין כרגע משהו שעוד לא התחלת")
        return ok(card(random.choice(pool)))

    async def genres_list(request: Request):
        """Genres in the library with how many titles have each: ?kind=series|movie"""
        kind = request.query_params.get("kind", "")
        counts: dict[str, int] = {}
        for t in db.library_titles():
            if kind in ("series", "movie") and t["kind"] != kind:
                continue
            for g in t["genres"]:
                counts[g] = counts.get(g, 0) + 1
        st = scanner.tmdb.status if scanner.tmdb else None
        return ok({"genres": [{"name": g, "count": n} for g, n in sorted(counts.items(), key=lambda x: -x[1])],
                   "loading": st if st and st.get("running") else None})

    async def watched_many(request: Request):
        """Mark several episodes at once (a whole season, or 'up to here'). Returns their previous state for undo."""
        data = await body(request)
        ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()][:2000]
        watched = bool(data.get("watched"))
        before = {m["id"]: bool(m["completed"]) for m in (db.get_media(i) for i in ids) if m}
        for i in before:
            db.mark_watched(i, watched)
        return ok({"ok": True, "before": before})

    async def title_detail(request: Request):
        title_id = request.path_params["id"]
        t = db.get_title(title_id)
        if not t:
            raise ApiError(404, "לא נמצא")
        if scanner.tmdb and not t["tmdb_checked"]:  # fetch the poster now instead of waiting for the queue
            any_media = db.q1("SELECT id FROM media WHERE title_id = ?", (title_id,))
            try:
                await asyncio.wait_for(scanner.tmdb.enrich_title(db, t), 8)
            except (asyncio.TimeoutError, httpx.TransportError):  # slow or no internet: show the page anyway
                pass
            t = db.get_title(title_id)
            if not t and any_media:  # merged into an older entry of the same movie/series
                title_id = db.get_media(any_media["id"])["title_id"]
                t = db.get_title(title_id)
            if not t:
                raise ApiError(404, "לא נמצא")
        hidden = request.query_params.get("hidden") in ("1", "true")
        info = await episode_names(t)
        items = [media_out(with_names(m, info)) for m in db.title_media(title_id, include_hidden=hidden)]
        if not t["poster"] and items:
            t["poster"] = items[0]["thumb"]
        resume = db.resume_point(title_id)
        t = dict(t)
        for k, default in (("genres", []), ("cast_json", [])):
            try:
                t[k] = json.loads(t.get(k) or "null") or default
            except ValueError:
                t[k] = default
        t["cast"] = t.pop("cast_json")
        return ok({
            "title": t,
            "new_since": db.last_watched_at(title_id) if t["kind"] == "series" else None,
            "in_list": title_id in db.watchlist_ids(),
            "ep_names": t["kind"] == "series" and bool(t.get("tmdb_id")),
            "ep_names_off": title_id in names_off(),
            "hidden_title": bool(t.get("hidden_title")),
            "season_labels": db.season_labels(title_id),
            "media": items,
            "resume": {"media": media_out(resume["media"]), "position": resume["position"], "state": resume["state"]}
            if resume else None,
        })

    async def edit_title(request: Request):
        title_id = request.path_params["id"]
        if not db.get_title(title_id):
            raise ApiError(404)
        payload = await body(request)
        fields = {}
        if payload.get("name"):
            fields["name"] = str(payload["name"]).strip()
            if fields["name"] != db.get_title(title_id)["name"]:
                fields["name_manual"] = 1  # your name stays, even when TMDB info is fetched again
        if payload.get("kind") in ("series", "movie"):
            fields["kind"] = payload["kind"]
        if "year" in payload:
            fields["year"] = int(payload["year"]) if str(payload["year"] or "").isdigit() else None
        if "overview" in payload:
            fields["overview"] = payload["overview"]
        if "poster" in payload:
            fields["poster"] = payload["poster"] or None
        if "hidden_title" in payload:  # hide from the whole library (or show again)
            fields["hidden_title"] = 1 if payload["hidden_title"] else 0
        if "ep_names_off" in payload:
            off = names_off()
            off.add(title_id) if payload["ep_names_off"] else off.discard(title_id)
            db.set_pref("ep_names_off", json.dumps(sorted(off)))
        for key in ("intro_start", "intro_end"):  # the opening of a series, for "skip intro" (seconds; null = clear)
            if key in payload:
                try:
                    fields[key] = max(0.0, float(payload[key])) if payload[key] is not None else None
                except (TypeError, ValueError) as e:
                    raise ApiError(400, "bad values") from e
        try:
            db.update_title(title_id, soft=set(fields) <= {"intro_start", "intro_end"}, **fields)
        except Exception as e:  # noqa: BLE001 - e.g. unique (kind, norm_name) clash
            raise ApiError(400, str(e)) from e
        tmdb = scanner.tmdb
        try:
            if payload.get("tmdb_id") and tmdb:
                m = re.search(r"\d+", str(payload["tmdb_id"]))
                if m:
                    await tmdb.enrich_title(db, db.get_title(title_id), tmdb_id=int(m.group()))
            elif payload.get("refresh_tmdb") and tmdb:
                await tmdb.enrich_title(db, db.get_title(title_id))
        except httpx.TransportError as e:
            raise ApiError(503, "אין חיבור לאתר TMDB כרגע. בדוק את האינטרנט ונסה שוב.") from e
        return ok(db.get_title(title_id))

    async def title_channels(request: Request):
        return ok({"channels": db.title_channels(request.path_params["id"])})

    async def split_title(request: Request):
        """Your choice: this group's videos of the title are a different show - give them their own title."""
        title_id = request.path_params["id"]
        chat_id = int((await body(request)).get("chat_id") or 0)
        chans = db.title_channels(title_id)
        if len(chans) < 2 or chat_id not in {c["chat_id"] for c in chans}:
            raise ApiError(400, "אין מה לפצל")
        new_id = db.split_by_channel(title_id, chat_id)
        if scanner.tmdb:  # the new one finds its own poster
            asyncio.ensure_future(scanner.tmdb.enrich_title(db, db.get_title(new_id)))
        return ok({"title_id": new_id})

    async def merge_title(request: Request):
        title_id = request.path_params["id"]
        target = int((await body(request)).get("into") or 0)
        if target == title_id or not db.get_title(target) or not db.get_title(title_id):
            raise ApiError(400, "יעד לא תקין")
        db.x("UPDATE media SET manual = 1 WHERE title_id = ?", (title_id,))  # your merge: never undone
        db.merge_titles(title_id, target)
        return ok({"ok": True, "title_id": target})

    async def edit_media(request: Request):
        media_id = request.path_params["id"]
        m = db.get_media(media_id)
        if not m:
            raise ApiError(404)
        payload = await body(request)
        fields = {}
        for key in ("season", "episode"):
            if key in payload:
                v = payload[key]
                fields[key] = int(v) if str(v or "").isdigit() else None
        if "hidden" in payload:
            fields["hidden"] = int(bool(payload["hidden"]))
        if payload.get("move_to_name"):
            name = str(payload["move_to_name"]).strip()
            kind = payload.get("kind") if payload.get("kind") in ("series", "movie") else db.get_title(m["title_id"])["kind"]
            fields["title_id"] = db.get_or_create_title(kind, name, normalize(name) or name, None)
        if {"season", "episode", "title_id"} & fields.keys():
            fields["manual"] = 1
        db.update_media(media_id, **fields)
        db.delete_empty_titles()
        return ok(media_out(db.get_media(media_id)))

    async def media_detail(request: Request):
        media_id = request.path_params["id"]
        m = db.get_media(media_id)
        if not m:
            raise ApiError(404, "לא נמצא")
        prev, nxt = db.neighbours(media_id)
        versions = db.versions(media_id)
        watched = [v for v in versions if v.get("watched_at")]
        if watched:  # progress is shared between versions of the same episode
            last = max(watched, key=lambda v: v["watched_at"])
            m = dict(m, position=last["position"], completed=max(v["completed"] or 0 for v in watched))
        t = db.get_title(m["title_id"])
        info = await episode_names(t, wait=3)
        ip = lan_ip()  # the address a TV on the same Wi-Fi uses to fetch this video (Chromecast)
        cast_url = (f"http://{ip}:{request.url.port or os.getenv('PORT', '8000')}/api/stream/{m['id']}?t={cast_token(m['id'])}"
                    if ip else None)
        return ok({
            "cast_url": cast_url,
            "media": media_out(with_names(dict(m), info)),
            "title": t,
            "prev": media_out(with_names(dict(prev), info)) if prev else None,
            "next": media_out(with_names(dict(nxt), info)) if nxt else None,
            "versions": [{"id": v["id"], "quality": v["quality"], "size": v["size"]} for v in db.versions(media_id)],
        })

    # -------------------------------------------------------------- progress
    async def save_progress(request: Request):
        media_id = request.path_params["id"]
        payload = await body(request)
        try:
            position = float(payload.get("position") or 0)
            duration = float(payload["duration"]) if payload.get("duration") else None
        except (TypeError, ValueError) as e:
            raise ApiError(400, "bad values") from e
        if not db.get_media(media_id):
            raise ApiError(404)
        return ok({"ok": True, "completed": db.save_progress(media_id, max(0.0, position), duration)})

    async def set_watched(request: Request):
        media_id = request.path_params["id"]
        if not db.get_media(media_id):
            raise ApiError(404)
        db.mark_watched(media_id, bool((await body(request)).get("watched")))
        return ok()

    # ------------------------------------------------------------- streaming
    range_re = re.compile(r"bytes=(\d*)-(\d*)")

    async def stream(request: Request):
        m = db.get_media(request.path_params["id"])
        if not m:
            raise ApiError(404)
        if not tg.authorized:
            raise ApiError(503, "טלגרם לא מחובר")
        size = m["size"]
        start, end, status = 0, size - 1, 200
        rng = request.headers.get("range")
        if rng:
            match = range_re.match(rng.strip())
            if match:
                s, e = match.groups()
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:  # suffix range: last N bytes
                    start = max(0, size - int(e))
                end = min(end, size - 1)
                status = 206
            if start >= size or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        try:
            await tg.get_message(m["chat_id"], m["msg_id"])
        except FileNotFoundError as e:
            raise ApiError(410, "הקובץ נמחק מטלגרם") from e
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "private, max-age=3600",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(tg.stream(m["chat_id"], m["msg_id"], start, end), status_code=status,
                                 headers=headers, media_type=m["mime"] or "video/mp4")

    async def thumb(request: Request):
        media_id = request.path_params["id"]
        path = settings.data_dir / "thumbs" / f"{media_id}.jpg"
        missing = settings.data_dir / "thumbs" / f"{media_id}.none"
        no_picture = {"Cache-Control": "private, max-age=86400"}  # the browser stops asking for a day
        if not path.exists():
            m = db.get_media(media_id)
            if not m or missing.exists():
                raise ApiError(404, headers=no_picture)
            if not tg.authorized:
                raise ApiError(503)
            try:  # never let one slow picture hold the browser's connections (it only has a few)
                data = await asyncio.wait_for(tg.thumbnail(m["chat_id"], m["msg_id"]), 12)
            except asyncio.TimeoutError:
                asyncio.ensure_future(tg.heal())  # pictures stuck: maybe the file connection went stale
                raise ApiError(504) from None  # try again next time; not marked as missing
            except Exception as e:  # noqa: BLE001 - e.g. no connection right now: try again next time
                log.info("picture %s not loaded now: %s", media_id, type(e).__name__)
                raise ApiError(503) from None
            if not data:  # the video really has no picture: remember, never ask Telegram again
                missing.touch()
                raise ApiError(404, headers=no_picture)
            path.write_bytes(data)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    # -------------------------------------------------------------- settings
    async def safe_me():
        if not tg.authorized:
            return None
        try:
            return await asyncio.wait_for(tg.me(), 10)
        except Exception as e:  # noqa: BLE001 - e.g. the phone switched networks; Telethon reconnects
            log.warning("could not reach Telegram: %s", e)
            return {"name": "(אין חיבור כרגע, מתחבר מחדש...)", "username": None}

    # ---------------------------------------------------------- device sync
    def sync_key() -> str:
        """Both devices use the same site password (config.json was copied), so they derive the same key."""
        base = settings.password_hash or settings.env_password
        return hashlib.sha256(f"tgv-sync:{base}".encode()).hexdigest() if base else ""

    def peer_url(raw: str) -> str:
        raw = raw.strip().rstrip("/")
        if not raw:
            return ""
        if not re.match(r"https?://", raw):
            raw = "http://" + raw
        if not re.search(r":\d+$", raw.split("//", 1)[1]):
            raw += ":8000"
        return raw

    def this_device_address() -> str | None:
        if os.getenv("HOST") != "0.0.0.0":
            return None  # only this device can open the site (the phone's default)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                sk.connect(("10.255.255.255", 1))
                ip = sk.getsockname()[0]
            return None if ip.startswith("127.") else f"http://{ip}:{os.getenv('PORT', '8000')}"
        except OSError:
            return None

    sync_state: dict = {"running": False}

    async def sync_exchange(request: Request):
        """Called by the OTHER device: take its progress, answer with ours."""
        key = sync_key()
        if not key or not hmac.compare_digest(request.headers.get("x-sync-key", ""), key):
            raise ApiError(403, "מפתח סנכרון שגוי: הסיסמה של האתר צריכה להיות זהה בשני המכשירים")
        data = await body(request)
        got = await asyncio.to_thread(db.import_progress, data.get("rows") or [])
        db.set_pref("sync_last_from", json.dumps({"t": time.time(), "got": got}))
        return ok({"rows": db.export_progress(), "now": time.time(), "got": got})

    async def run_sync() -> dict:
        peer = peer_url(db.get_pref("sync_peer"))
        if not peer or sync_state["running"]:
            return sync_state
        sync_state["running"] = True
        result: dict = {"t": time.time(), "peer": peer}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(peer + "/api/sync/exchange", headers={"x-sync-key": sync_key()},
                                      json={"rows": db.export_progress()})
            if r.status_code == 403:
                raise RuntimeError("הסיסמה של האתר שונה בין המכשירים. הגדר אותה סיסמה בשניהם")
            r.raise_for_status()
            answer = r.json()
            result.update(ok=True, got=await asyncio.to_thread(db.import_progress, answer.get("rows") or []),
                          sent=answer.get("got", 0))
        except httpx.HTTPError:
            result.update(ok=False, error="המכשיר השני לא זמין. ודא שהאתר פועל בו ושאתם באותו Wi-Fi")
        except Exception as e:  # noqa: BLE001
            result.update(ok=False, error=str(e))
        finally:
            sync_state["running"] = False
        db.set_pref("sync_last", json.dumps(result, ensure_ascii=False))
        return result

    async def sync_now(request: Request):
        if not db.get_pref("sync_peer"):
            raise ApiError(400, "הזן קודם את הכתובת של המכשיר השני")
        return ok(await run_sync())

    async def periodic_sync() -> None:
        while True:
            await asyncio.sleep(SYNC_EVERY_SECONDS)
            if db.get_pref("sync_peer"):
                await run_sync()

    async def status(request: Request):
        return ok({
            "telegram_configured": tg.configured,
            "telegram_authorized": tg.authorized,
            "api_id": settings.api_id or None,
            "me": await safe_me(),
            "tmdb": bool(scanner.tmdb),
            "tmdb_status": scanner.tmdb.status if scanner.tmdb else None,
            "scan": scanner.status,
            "chats": db.chats(),
            "scan_interval_minutes": settings.scan_interval_minutes,
            "strip_phrases": db.strip_phrases(),
            "duplicates": len(possible_duplicates(db)),
            "sync": {
                "peer": db.get_pref("sync_peer"),
                "this_device": this_device_address(),
                "last": json.loads(db.get_pref("sync_last") or "null"),
                "last_from": json.loads(db.get_pref("sync_last_from") or "null"),
            },
        })

    async def save_settings(request: Request):
        payload = await body(request)
        if "tmdb_api_key" in payload:
            key = str(payload["tmdb_api_key"] or "").strip()
            settings.save(tmdb_api_key=key)
            if scanner.tmdb:
                scanner.tmdb.stopped = True  # stop the run that uses the old key
            scanner.tmdb = TMDB(key) if key else None
            if key:  # look up titles that were added before the key existed
                db.x("UPDATE titles SET tmdb_checked = 0 WHERE tmdb_id IS NULL")
                scanner.start_tmdb()
        if "sync_peer" in payload:
            db.set_pref("sync_peer", peer_url(str(payload["sync_peer"] or "")))
        if "strip_phrases" in payload:
            raw = str(payload["strip_phrases"] or "")
            phrases = [p.strip() for p in re.split(r"[\n,]+", raw) if p.strip()]
            db.set_pref("strip_phrases", "\n".join(phrases))
            from .library import regroup_all

            async def regroup() -> None:
                before = db.q1("SELECT COUNT(*) AS c FROM titles")["c"]
                scanner.status.update(running=True, chat="מסדר שמות לפי הרשימה שלך", added=0, error=None, note=None)
                try:
                    moved = await asyncio.to_thread(regroup_all, db, True)
                    after = db.q1("SELECT COUNT(*) AS c FROM titles")["c"]
                    scanner.status["note"] = (f"ניקוי שמות הסתיים: {moved:,} קבצים קיבלו שם נקי, "
                                              f"{max(0, before - after):,} כפילויות אוחדו")
                    log.info("strip phrases applied: %d videos re-filed, titles %d -> %d", moved, before, after)
                except Exception as e:  # noqa: BLE001 - show it in Settings instead of failing silently
                    log.exception("re-filing names failed")
                    scanner.status["error"] = f"ניקוי שמות נכשל: {type(e).__name__}: {e}"
                finally:
                    scanner.status.update(running=False, chat=None, last_finished=time.time())
                scanner.start_tmdb()
            asyncio.create_task(regroup())
        if "api_id" in payload or "api_hash" in payload:
            api_id = re.sub(r"\D", "", str(payload.get("api_id") or ""))
            api_hash = str(payload.get("api_hash") or "").strip()
            if not api_id or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
                raise ApiError(400, "API ID צריך להיות מספר, ו-API Hash צריך להיות 32 תווים (אותיות a-f ומספרים)")
            settings.save(api_id=api_id, api_hash=api_hash)
            try:
                await tg.start(int(api_id), api_hash)
            except Exception as e:  # noqa: BLE001
                raise ApiError(400, telegram_error_message(e)) from e
        return ok()

    async def tg_send_code(request: Request):
        raw = str((await body(request)).get("phone", "")).strip()
        # keep only digits (invisible direction marks from copy/paste are dropped) and a leading +
        digits = "".join(str(unicodedata.digit(c)) for c in raw if c.isdigit())
        phone = ("+" if raw.lstrip("\u200e\u200f\u202a\u202b\u202c\u2066\u2067\u2068\u2069 ").startswith("+") else "") + digits
        if phone.startswith("00"):
            phone = "+" + phone[2:]
        if phone.startswith("0"):
            phone = "+972" + phone[1:]  # Israeli local format
        if not re.fullmatch(r"\+?\d{8,15}", phone):
            raise ApiError(400, "מספר טלפון לא תקין")
        try:
            how = await tg.send_code(phone)
        except Exception as e:  # noqa: BLE001
            raise ApiError(400, telegram_error_message(e)) from e
        return ok({"ok": True, "phone": phone, "already_sent": how == "already_sent"})

    async def tg_sign_in(request: Request):
        data = await body(request)
        try:
            if data.get("password"):
                await tg.sign_in_password(str(data["password"]))
                done = True
            else:
                done = await tg.sign_in_code(re.sub(r"\D", "", str(data.get("code", ""))))
        except Exception as e:  # noqa: BLE001
            raise ApiError(400, telegram_error_message(e)) from e
        return ok({"ok": True, "done": done, "need_password": not done})

    async def tg_logout(request: Request):
        await tg.log_out()
        return ok()

    async def dialogs(request: Request):
        if not tg.authorized:
            raise ApiError(503, "טלגרם לא מחובר")
        selected = {c["id"]: c for c in db.chats()}
        out = await tg.list_dialogs()
        for d in out:
            saved = selected.get(d["id"], {})
            d["enabled"] = bool(saved.get("enabled"))
            d["season"] = saved.get("default_season")
            d["season_label"] = saved.get("season_label")
            d["origin"] = saved.get("origin") or ""
        return ok(out)

    async def set_chats(request: Request):
        for c in (await body(request)).get("chats", []):
            chat_id = int(c["id"])
            season = str(c.get("season") or "").strip()
            season = int(season) if season.isdigit() and int(season) > 0 else None
            old = db.get_chat(chat_id)
            origin = c.get("origin") if c.get("origin") in ("il", "intl") else None
            db.upsert_chat(chat_id, str(c.get("title") or c["id"]), bool(c.get("enabled")),
                           season, str(c.get("season_label") or "").strip() or None, origin)
            if old and (old.get("origin") or None) != origin:
                db.set_chat_branding(chat_id, None)  # re-file this group's shows with the new origin on the next scan
            if old and old.get("default_season") != season:
                db.apply_chat_season(chat_id)
        return ok(db.chats())

    async def start_scan(request: Request):
        if not tg.authorized:
            raise ApiError(503, "טלגרם לא מחובר")
        if (await body(request)).get("full"):
            for c in db.chats():
                db.reset_chat(c["id"])
        if scanner.status.get("running"):
            scanner._again = True  # busy (e.g. sorting names after an update): scan again as soon as it ends
            scanner.status["queued"] = True
            return ok({**scanner.status, "queued": True})
        asyncio.create_task(scanner.scan())
        await asyncio.sleep(0)
        return ok(scanner.status)

    # ------------------------------------------------------------- lifecycle
    async def periodic_scan() -> None:
        while True:
            await asyncio.sleep(max(1, settings.scan_interval_minutes) * 60)
            if tg.authorized:
                await scanner.scan()

    async def startup_jobs() -> None:
        """Library upkeep runs AFTER the site is up, so a big library never keeps the site from opening."""
        from .library import migrate_norms, repair
        scanner.status.update(running=True, chat="מסדר את הספרייה (פעם אחת אחרי עדכון)")
        try:
            await asyncio.to_thread(migrate_norms, db)
            await asyncio.to_thread(repair, db)
            from .library import merge_similar_movies, recheck_foreign_matches, refresh_series_years, retry_unmatched

            retry_unmatched(db)
            recheck_foreign_matches(db)
            if db.get_pref("ep_names_reset") != "1":  # names saved before a TMDB link was changed by hand: fetch again
                with db._lock:
                    db.conn.execute("DELETE FROM episode_info")
                    db.conn.execute("DELETE FROM episode_seasons")
                    db.conn.commit()
                db.set_pref("ep_names_reset", "1")
            refresh_series_years(db)
            if db.get_pref("thumbs_retry") != "1":  # pictures wrongly marked "none" during connection problems
                for f in (settings.data_dir / "thumbs").glob("*.none"):
                    f.unlink(missing_ok=True)
                db.set_pref("thumbs_retry", "1")
            await asyncio.to_thread(merge_similar_movies, db)
            from .library import split_mixed_titles

            await asyncio.to_thread(split_mixed_titles, db)
        except Exception as e:  # noqa: BLE001 - never take the site down for this; show it in Settings
            log.exception("library upkeep failed")
            scanner.status["error"] = f"סידור הספרייה נכשל: {type(e).__name__}: {e}"
        finally:
            scanner.status.update(running=False, chat=None)
        if tg.authorized:
            await scanner.scan()  # pick up new episodes on start
        elif scanner.tmdb:
            scanner.start_tmdb()

    def watch_event_loop(loop) -> None:
        """Diagnostics: if the site's main loop gets stuck (every page waits), print WHAT it is doing."""
        import sys
        import threading
        import traceback

        beat = {"t": time.monotonic()}

        def tick():
            beat["t"] = time.monotonic()
            loop.call_later(0.5, tick)

        loop.call_soon_threadsafe(tick)
        main_id = threading.get_ident()

        def watch():
            reported = 0.0
            while True:
                time.sleep(1)
                stuck = time.monotonic() - beat["t"]
                if stuck > 2.5 and time.monotonic() - reported > 30:
                    reported = time.monotonic()
                    frame = sys._current_frames().get(main_id)
                    where = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
                                        for f in reversed(traceback.extract_stack(frame)[-6:])) if frame else "?"
                    log.warning("site busy for %.1f s in: %s", stuck, where)

        threading.Thread(target=watch, daemon=True, name="loop-watch").start()

    @asynccontextmanager
    async def lifespan(app):
        watch_event_loop(asyncio.get_running_loop())
        try:
            await asyncio.wait_for(tg.start(), 30)
        except Exception:  # noqa: BLE001 - site still works for browsing the library
            log.exception("could not connect to Telegram")
        task = asyncio.create_task(periodic_scan()) if settings.scan_interval_minutes > 0 else None
        syncer = asyncio.create_task(periodic_sync())
        jobs = asyncio.create_task(startup_jobs())
        yield
        jobs.cancel()
        syncer.cancel()
        if task:
            task.cancel()
        await tg.stop()

    async def api_error(request: Request, exc: ApiError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status, headers=exc.headers)

    async def server_error(request: Request, exc: Exception):
        """Show what went wrong instead of a bare 500, and keep a log in data/errors.log."""
        import traceback

        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        log.error("error on %s\n%s", request.url.path, tb)
        try:
            path = settings.data_dir / "errors.log"
            old = path.read_text(encoding="utf-8")[-20000:] if path.exists() else ""
            path.write_text(old + f"\n--- {time.ctime()} {request.url.path}\n{tb}", encoding="utf-8")
        except OSError:
            pass
        return JSONResponse({"detail": f"שגיאה בשרת ({request.url.path}): {type(exc).__name__}: {exc}"},
                            status_code=500)

    R = Route
    routes = [
        R("/", index), R("/login", login_page), R("/manifest.json", manifest),
        R("/api/setup-state", setup_state), R("/api/setup", setup, methods=["POST"]),
        R("/api/login", login, methods=["POST"]), R("/api/logout", logout, methods=["POST"]),
        R("/api/password", change_password, methods=["POST"]),
        R("/api/library", library), R("/api/genres", genres_list), R("/api/home", home_rows), R("/api/random", random_pick),
        R("/api/mylist", my_list), R("/api/watchlist/{id:int}", watchlist_set, methods=["POST"]),
        R("/api/history/hide", history_hide, methods=["POST"]),
        R("/api/continue/hide", continue_hide, methods=["POST"]), R("/api/hidden", hidden_titles),
        R("/api/duplicates", duplicates), R("/api/duplicates/dismiss", duplicates_dismiss, methods=["POST"]),
        R("/api/watched-many", watched_many, methods=["POST"]),
        R("/api/titles/{id:int}", title_detail), R("/api/titles/{id:int}", edit_title, methods=["PATCH"]),
        R("/api/titles/{id:int}/merge", merge_title, methods=["POST"]),
        R("/api/titles/{id:int}/channels", title_channels), R("/api/titles/{id:int}/split", split_title, methods=["POST"]),
        R("/api/media/{id:int}", media_detail), R("/api/media/{id:int}", edit_media, methods=["PATCH"]),
        R("/api/progress/{id:int}", save_progress, methods=["POST"]),
        R("/api/watched/{id:int}", set_watched, methods=["POST"]),
        R("/api/stream/{id:int}", stream), R("/api/thumb/{id:int}", thumb),
        R("/api/status", status), R("/api/settings", save_settings, methods=["POST"]),
        R("/api/tg/send-code", tg_send_code, methods=["POST"]),
        R("/api/tg/sign-in", tg_sign_in, methods=["POST"]),
        R("/api/tg/logout", tg_logout, methods=["POST"]),
        R("/api/dialogs", dialogs), R("/api/chats", set_chats, methods=["POST"]),
        R("/api/scan", start_scan, methods=["POST"]),
        R("/api/sync/exchange", sync_exchange, methods=["POST"]), R("/api/sync/now", sync_now, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={ApiError: api_error, Exception: server_error})
    app.add_middleware(AuthMiddleware, settings=settings, valid_token=valid_token, valid_cast=valid_cast)
    app.state.db, app.state.tg, app.state.scanner, app.state.settings = db, tg, scanner, settings
    return app
