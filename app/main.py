"""Web server: private site with library, player, progress tracking and settings."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import Database
from .parser import normalize
from .telegram import Scanner, TelegramService
from .tmdb import TMDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC = Path(__file__).parent / "static"
COOKIE = "tgv_session"
SESSION_DAYS = 90


def create_app(settings: Settings | None = None, tg: TelegramService | None = None) -> FastAPI:
    settings = settings or Settings()
    db = Database(settings.db_path)
    tg = tg or TelegramService(settings.api_id, settings.api_hash, str(settings.session_path))
    tmdb = TMDB(settings.tmdb_api_key) if settings.tmdb_api_key else None
    scanner = Scanner(db, tg, tmdb, settings.min_duration_seconds)
    secret = settings.secret_key.encode()

    async def periodic_scan() -> None:
        while True:
            await asyncio.sleep(max(1, settings.scan_interval_minutes) * 60)
            if tg.authorized:
                await scanner.scan()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await tg.start()
        except Exception:  # noqa: BLE001 - site still works for browsing the library
            log.exception("could not connect to Telegram")
        task = asyncio.create_task(periodic_scan()) if settings.scan_interval_minutes > 0 else None
        yield
        if task:
            task.cancel()
        await tg.stop()

    app = FastAPI(title="TG Video", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.db, app.state.tg, app.state.scanner = db, tg, scanner

    # ------------------------------------------------------------------ auth
    def make_token() -> str:
        exp = str(int(time.time()) + SESSION_DAYS * 86400)
        sig = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def valid_token(token: str | None) -> bool:
        if not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        good = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        public = path in ("/login", "/api/login") or path.startswith("/static/")
        if not public and not valid_token(request.cookies.get(COOKIE)):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "login required"}, status_code=401)
            return RedirectResponse("/login")
        return await call_next(request)

    _attempts: list[float] = []

    @app.post("/api/login")
    async def login(payload: dict = Body(...)):
        now = time.time()
        _attempts[:] = [t for t in _attempts if now - t < 60]
        if len(_attempts) >= 10:
            raise HTTPException(429, "יותר מדי ניסיונות, נסה שוב בעוד דקה")
        _attempts.append(now)
        if not settings.site_password:
            raise HTTPException(500, "SITE_PASSWORD לא הוגדר בקובץ .env")
        if not hmac.compare_digest(str(payload.get("password", "")), settings.site_password):
            raise HTTPException(401, "סיסמה שגויה")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, make_token(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    # ----------------------------------------------------------------- pages
    @app.get("/login")
    async def login_page():
        return FileResponse(STATIC / "login.html")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

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
        }

    @app.get("/api/library")
    async def library():
        titles = []
        for t in db.list_titles():
            t["poster"] = t["poster"] or thumb_url(t["first_media_id"])
            titles.append(t)
        cont = [
            {
                "title": {"id": c["title"]["id"], "name": c["title"]["name"], "kind": c["title"]["kind"],
                          "backdrop": c["title"]["backdrop"], "poster": c["title"]["poster"]},
                "media": media_out(c["media"]),
                "position": c["position"],
                "state": c["state"],
            }
            for c in db.continue_watching()
        ]
        return {"titles": titles, "continue": cont}

    @app.get("/api/titles/{title_id}")
    async def title_detail(title_id: int, hidden: bool = False):
        t = db.get_title(title_id)
        if not t:
            raise HTTPException(404, "לא נמצא")
        items = [media_out(m) for m in db.title_media(title_id, include_hidden=hidden)]
        if not t["poster"] and items:
            t["poster"] = items[0]["thumb"]
        resume = db.resume_point(title_id)
        return {
            "title": t,
            "media": items,
            "resume": {"media": media_out(resume["media"]), "position": resume["position"], "state": resume["state"]}
            if resume else None,
        }

    @app.patch("/api/titles/{title_id}")
    async def edit_title(title_id: int, payload: dict = Body(...)):
        t = db.get_title(title_id)
        if not t:
            raise HTTPException(404)
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
        except Exception as e:  # unique (kind, norm_name) clash etc.
            raise HTTPException(400, str(e)) from e
        if payload.get("tmdb_id") and tmdb:
            tid = int(re.sub(r"\D", "", str(payload["tmdb_id"])) or 0)
            if tid:
                await tmdb.enrich_title(db, db.get_title(title_id), tmdb_id=tid)
        elif payload.get("refresh_tmdb") and tmdb:
            await tmdb.enrich_title(db, db.get_title(title_id))
        return db.get_title(title_id)

    @app.post("/api/titles/{title_id}/merge")
    async def merge_title(title_id: int, payload: dict = Body(...)):
        target = int(payload.get("into", 0))
        if target == title_id or not db.get_title(target) or not db.get_title(title_id):
            raise HTTPException(400, "יעד לא תקין")
        db.merge_titles(title_id, target)
        return {"ok": True, "title_id": target}

    @app.patch("/api/media/{media_id}")
    async def edit_media(media_id: int, payload: dict = Body(...)):
        m = db.get_media(media_id)
        if not m:
            raise HTTPException(404)
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
        db.update_media(media_id, **fields)
        db.delete_empty_titles()
        return media_out(db.get_media(media_id))

    @app.get("/api/media/{media_id}")
    async def media_detail(media_id: int):
        m = db.get_media(media_id)
        if not m:
            raise HTTPException(404, "לא נמצא")
        prev, nxt = db.neighbours(media_id)
        return {
            "media": media_out(m),
            "title": db.get_title(m["title_id"]),
            "prev": media_out(prev) if prev else None,
            "next": media_out(nxt) if nxt else None,
        }

    # -------------------------------------------------------------- progress
    @app.post("/api/progress/{media_id}")
    async def save_progress(media_id: int, payload: dict = Body(...)):
        try:
            position = float(payload.get("position", 0))
            duration = float(payload["duration"]) if payload.get("duration") else None
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "bad values") from e
        if not db.get_media(media_id):
            raise HTTPException(404)
        completed = db.save_progress(media_id, max(0.0, position), duration)
        return {"ok": True, "completed": completed}

    @app.post("/api/watched/{media_id}")
    async def set_watched(media_id: int, payload: dict = Body(...)):
        if not db.get_media(media_id):
            raise HTTPException(404)
        db.mark_watched(media_id, bool(payload.get("watched")))
        return {"ok": True}

    # ------------------------------------------------------------- streaming
    RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

    @app.get("/api/stream/{media_id}")
    async def stream(media_id: int, request: Request):
        m = db.get_media(media_id)
        if not m:
            raise HTTPException(404)
        if not tg.authorized:
            raise HTTPException(503, "טלגרם לא מחובר")
        size = m["size"]
        start, end, status = 0, size - 1, 200
        rng = request.headers.get("range")
        if rng:
            match = RANGE_RE.match(rng.strip())
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
            raise HTTPException(410, "הקובץ נמחק מטלגרם") from e
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "private, max-age=3600",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        mime = m["mime"] or "video/mp4"
        return StreamingResponse(tg.stream(m["chat_id"], m["msg_id"], start, end), status_code=status,
                                 headers=headers, media_type=mime)

    @app.get("/api/thumb/{media_id}")
    async def thumb(media_id: int):
        path = settings.data_dir / "thumbs" / f"{media_id}.jpg"
        missing = settings.data_dir / "thumbs" / f"{media_id}.none"
        if not path.exists():
            m = db.get_media(media_id)
            if not m or missing.exists() or not tg.authorized:
                raise HTTPException(404)
            try:
                data = await tg.thumbnail(m["chat_id"], m["msg_id"])
            except Exception:  # noqa: BLE001
                data = None
            if not data:
                missing.touch()
                raise HTTPException(404)
            path.write_bytes(data)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    # -------------------------------------------------------------- settings
    @app.get("/api/status")
    async def status():
        return {
            "telegram_configured": tg.configured,
            "telegram_authorized": tg.authorized,
            "me": await tg.me() if tg.authorized else None,
            "tmdb": bool(tmdb),
            "scan": scanner.status,
            "chats": db.chats(),
            "scan_interval_minutes": settings.scan_interval_minutes,
        }

    @app.get("/api/dialogs")
    async def dialogs():
        if not tg.authorized:
            raise HTTPException(503, "טלגרם לא מחובר. הרץ: python -m app.login")
        selected = {c["id"]: c for c in db.chats()}
        out = await tg.list_dialogs()
        for d in out:
            d["enabled"] = bool(selected.get(d["id"], {}).get("enabled"))
        return out

    @app.post("/api/chats")
    async def set_chats(payload: dict = Body(...)):
        for c in payload.get("chats", []):
            db.upsert_chat(int(c["id"]), str(c.get("title") or c["id"]), bool(c.get("enabled")))
        return db.chats()

    @app.post("/api/scan")
    async def start_scan(payload: dict = Body(default={})):
        if not tg.authorized:
            raise HTTPException(503, "טלגרם לא מחובר")
        if payload.get("full"):
            for c in db.chats():
                db.reset_chat(c["id"])
        if not scanner.status.get("running"):
            asyncio.create_task(scanner.scan())
            await asyncio.sleep(0)
        return scanner.status

    return app

