"""Web server: private site with library, player, progress tracking and settings.

Built on Starlette (pure Python) so it installs on Android/Termux without compiling.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import crypto_boost
from .config import Settings
from .db import Database, quality_of
from .parser import normalize
from .telegram import Scanner, TelegramService
from .tmdb import TMDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
logging.getLogger("telethon").setLevel(logging.WARNING)  # keep the Termux screen readable
logging.getLogger("httpx").setLevel(logging.WARNING)     # its request log would print the TMDB key

STATIC = Path(__file__).parent / "static"
COOKIE = "tgv_session"
SESSION_DAYS = 365
PUBLIC_PATHS = {"/login", "/api/login", "/api/setup", "/api/setup-state", "/manifest.json"}


class ApiError(Exception):
    def __init__(self, status: int, detail: str = ""):
        self.status, self.detail = status, detail or "שגיאה"


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
    }.get(name, str(e) or name)


class AuthMiddleware:
    """Pure ASGI middleware (keeps video streaming efficient)."""

    def __init__(self, app, settings: Settings, valid_token):
        self.app, self.settings, self.valid_token = app, settings, valid_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await self.app(scope, receive, send)
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
    async def login_page(request: Request):
        return FileResponse(STATIC / "login.html")

    async def index(request: Request):
        return FileResponse(STATIC / "index.html")

    async def manifest(request: Request):
        return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")

    # --------------------------------------------------------------- library
    def thumb_url(media_id: int | None) -> str | None:
        return f"/api/thumb/{media_id}" if media_id else None

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
            "versions": m.get("versions", 1),
        }

    async def library(request: Request):
        """Paged library: ?kind=all|series|movie&q=search&offset=0&limit=60"""
        qp = request.query_params
        kind = qp.get("kind", "all")
        q = normalize(qp.get("q", "").strip())
        offset = max(0, int(qp.get("offset", 0) or 0))
        limit = min(200, max(1, int(qp.get("limit", 60) or 60)))
        items = db.library_titles()
        if q:
            items = [t for t in items if q in normalize(t["name"])]
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
        counts = {"all": len(items), "series": sum(1 for t in items if t["kind"] == "series")}
        counts["movie"] = counts["all"] - counts["series"]
        if kind in ("series", "movie"):
            items = [t for t in items if t["kind"] == kind]
        page = []
        for t in items[offset:offset + limit]:
            t = dict(t)
            t["poster"] = t["poster"] or thumb_url(t["first_media_id"])
            page.append(t)
        out = {"titles": page, "total": len(items), "counts": counts, "offset": offset}
        if offset == 0 and not q:
            out["continue"] = [
                {
                    "title": {k: c["title"][k] for k in ("id", "name", "kind", "backdrop", "poster")},
                    "media": media_out(c["media"]),
                    "position": c["position"],
                    "state": c["state"],
                }
                for c in db.continue_watching()
            ]
        return ok(out)

    async def title_detail(request: Request):
        title_id = request.path_params["id"]
        t = db.get_title(title_id)
        if not t:
            raise ApiError(404, "לא נמצא")
        if scanner.tmdb and not t["tmdb_checked"]:  # fetch the poster now instead of waiting for the queue
            any_media = db.q1("SELECT id FROM media WHERE title_id = ?", (title_id,))
            try:
                await asyncio.wait_for(scanner.tmdb.enrich_title(db, t), 8)
            except asyncio.TimeoutError:
                pass
            t = db.get_title(title_id)
            if not t and any_media:  # merged into an older entry of the same movie/series
                title_id = db.get_media(any_media["id"])["title_id"]
                t = db.get_title(title_id)
            if not t:
                raise ApiError(404, "לא נמצא")
        hidden = request.query_params.get("hidden") in ("1", "true")
        items = [media_out(m) for m in db.title_media(title_id, include_hidden=hidden)]
        if not t["poster"] and items:
            t["poster"] = items[0]["thumb"]
        resume = db.resume_point(title_id)
        return ok({
            "title": t,
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
        if payload.get("kind") in ("series", "movie"):
            fields["kind"] = payload["kind"]
        if "year" in payload:
            fields["year"] = int(payload["year"]) if str(payload["year"] or "").isdigit() else None
        if "overview" in payload:
            fields["overview"] = payload["overview"]
        if "poster" in payload:
            fields["poster"] = payload["poster"] or None
        try:
            db.update_title(title_id, **fields)
        except Exception as e:  # noqa: BLE001 - e.g. unique (kind, norm_name) clash
            raise ApiError(400, str(e)) from e
        tmdb = scanner.tmdb
        if payload.get("tmdb_id") and tmdb:
            m = re.search(r"\d+", str(payload["tmdb_id"]))
            if m:
                await tmdb.enrich_title(db, db.get_title(title_id), tmdb_id=int(m.group()))
        elif payload.get("refresh_tmdb") and tmdb:
            await tmdb.enrich_title(db, db.get_title(title_id))
        return ok(db.get_title(title_id))

    async def merge_title(request: Request):
        title_id = request.path_params["id"]
        target = int((await body(request)).get("into") or 0)
        if target == title_id or not db.get_title(target) or not db.get_title(title_id):
            raise ApiError(400, "יעד לא תקין")
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
        return ok({
            "media": media_out(m),
            "title": db.get_title(m["title_id"]),
            "prev": media_out(prev) if prev else None,
            "next": media_out(nxt) if nxt else None,
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
        if not path.exists():
            m = db.get_media(media_id)
            if not m or missing.exists() or not tg.authorized:
                raise ApiError(404)
            try:
                data = await tg.thumbnail(m["chat_id"], m["msg_id"])
            except Exception:  # noqa: BLE001
                data = None
            if not data:
                missing.touch()
                raise ApiError(404)
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
        if "strip_phrases" in payload:
            raw = str(payload["strip_phrases"] or "")
            phrases = [p.strip() for p in re.split(r"[\n,]+", raw) if p.strip()]
            db.set_pref("strip_phrases", "\n".join(phrases))
            from .library import regroup_all

            async def regroup() -> None:
                before = db.q1("SELECT COUNT(*) AS c FROM titles")["c"]
                scanner.status.update(running=True, chat="מסדר שמות לפי הרשימה שלך", added=0, error=None, note=None)
                try:
                    moved = await asyncio.to_thread(regroup_all, db)
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
        phone = re.sub(r"[\s\-()]", "", str((await body(request)).get("phone", "")))
        if phone.startswith("0"):
            phone = "+972" + phone[1:]  # Israeli local format
        if not re.fullmatch(r"\+?\d{8,15}", phone):
            raise ApiError(400, "מספר טלפון לא תקין")
        try:
            await tg.send_code(phone)
        except Exception as e:  # noqa: BLE001
            raise ApiError(400, telegram_error_message(e)) from e
        return ok({"ok": True, "phone": phone})

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
        return ok(out)

    async def set_chats(request: Request):
        for c in (await body(request)).get("chats", []):
            chat_id = int(c["id"])
            season = str(c.get("season") or "").strip()
            season = int(season) if season.isdigit() and int(season) > 0 else None
            old = db.get_chat(chat_id)
            db.upsert_chat(chat_id, str(c.get("title") or c["id"]), bool(c.get("enabled")),
                           season, str(c.get("season_label") or "").strip() or None)
            if old and old.get("default_season") != season:
                db.apply_chat_season(chat_id)
        return ok(db.chats())

    async def start_scan(request: Request):
        if not tg.authorized:
            raise ApiError(503, "טלגרם לא מחובר")
        if (await body(request)).get("full"):
            for c in db.chats():
                db.reset_chat(c["id"])
        if not scanner.status.get("running"):
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
        except Exception as e:  # noqa: BLE001 - never take the site down for this; show it in Settings
            log.exception("library upkeep failed")
            scanner.status["error"] = f"סידור הספרייה נכשל: {type(e).__name__}: {e}"
        finally:
            scanner.status.update(running=False, chat=None)
        if tg.authorized:
            await scanner.scan()  # pick up new episodes on start
        elif scanner.tmdb:
            scanner.start_tmdb()

    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.wait_for(tg.start(), 30)
        except Exception:  # noqa: BLE001 - site still works for browsing the library
            log.exception("could not connect to Telegram")
        task = asyncio.create_task(periodic_scan()) if settings.scan_interval_minutes > 0 else None
        jobs = asyncio.create_task(startup_jobs())
        yield
        jobs.cancel()
        if task:
            task.cancel()
        await tg.stop()

    async def api_error(request: Request, exc: ApiError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

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
        R("/api/library", library),
        R("/api/titles/{id:int}", title_detail), R("/api/titles/{id:int}", edit_title, methods=["PATCH"]),
        R("/api/titles/{id:int}/merge", merge_title, methods=["POST"]),
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
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={ApiError: api_error, Exception: server_error})
    app.add_middleware(AuthMiddleware, settings=settings, valid_token=valid_token)
    app.state.db, app.state.tg, app.state.scanner, app.state.settings = db, tg, scanner, settings
    return app
