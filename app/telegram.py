"""Telegram access via Telethon (MTProto): list groups, scan for videos, stream files in chunks."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

from telethon import TelegramClient
from telethon.errors import FileReferenceExpiredError

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024  # Telegram serves files in 1 MiB-aligned parts
MSG_CACHE_SECONDS = 20 * 60
DOWNLOAD_STALL_SECONDS = 25  # no data from Telegram for this long -> reconnect
NOT_VIDEO = "not-video"
# Telegram's ways of saying "you cannot read this group any more" (left it, banned, deleted, made private)
NO_ACCESS_ERRORS = {"ChannelPrivateError", "ChannelInvalidError", "ChatForbiddenError", "UserBannedInChannelError",
                    "ChatIdInvalidError", "PeerIdInvalidError", "ChannelBannedError", "ChatAdminRequiredError"}


def is_no_access(e: Exception) -> bool:
    return type(e).__name__ in NO_ACCESS_ERRORS or (isinstance(e, ValueError) and "entity" in str(e).lower())
VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts", ".wmv", ".flv")


@dataclass
class VideoMessage:
    chat_id: int
    msg_id: int
    caption: str
    file_name: str
    size: int
    mime: str
    duration: float | None
    width: int | None
    height: int | None
    date: float


def message_to_video(chat_id: int, msg) -> VideoMessage | None:
    f = getattr(msg, "file", None)
    if not f or not getattr(msg, "document", None):
        return None
    mime = (f.mime_type or "").lower()
    name = f.name or ""
    if not (mime.startswith("video/") or name.lower().endswith(VIDEO_EXTS)):
        return None
    if not mime.startswith("video/"):
        mime = "video/x-matroska" if name.lower().endswith(".mkv") else "video/mp4"
    return VideoMessage(
        chat_id=chat_id,
        msg_id=msg.id,
        caption=msg.message or "",
        file_name=name,
        size=f.size or 0,
        mime=mime,
        duration=getattr(f, "duration", None),
        width=getattr(f, "width", None),
        height=getattr(f, "height", None),
        date=msg.date.timestamp() if msg.date else time.time(),
    )


class TelegramService:
    def __init__(self, session_path: str, api_id: int = 0, api_hash: str = ""):
        self.session_path = session_path
        self.api_id, self.api_hash = api_id, api_hash
        self.client: TelegramClient | None = None
        self._msg_cache: dict[tuple[int, int], tuple[float, object]] = {}
        self.authorized = False
        self._phone: str | None = None
        self._phone_code_hash: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    async def start(self, api_id: int | None = None, api_hash: str | None = None) -> None:
        """(Re)connect, optionally with new API credentials."""
        if api_id is not None:
            self.api_id, self.api_hash = int(api_id or 0), api_hash or ""
        await self.stop()
        if not self.configured:
            log.warning("Telegram API credentials missing - set them in the website settings")
            return
        # keep retrying after the network drops (computer asleep, Wi-Fi change) instead of giving up for good
        self.client = TelegramClient(self.session_path, self.api_id, self.api_hash,
                                     connection_retries=500, retry_delay=5, auto_reconnect=True)
        if not getattr(self, "_watchdog", None) or self._watchdog.done():
            self._watchdog = asyncio.ensure_future(self._keep_connected())
        await self.client.connect()
        self.authorized = await self.client.is_user_authorized()
        if not self.authorized:
            log.warning("Telegram not logged in yet - log in from the website settings")

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
        self.client = None
        self.authorized = False

    # ------------------------------------------------------------ web login
    async def send_code(self, phone: str) -> str:
        if not self.client:
            raise RuntimeError("צריך קודם להזין API ID ו-API Hash")
        if not self.client.is_connected():
            await self.client.connect()
        try:
            sent = await self.client.send_code_request(phone)
        except Exception as e:  # noqa: BLE001
            # A code was already sent in this run and Telegram has no other way to resend it:
            # the code you already got is still the one to type, so go on to the code step.
            cached = getattr(self.client, "_phone_code_hash", {}).get(phone.lstrip("+"))
            if type(e).__name__ in ("SendCodeUnavailableError", "PhoneCodeExpiredError") and cached:
                self._phone, self._phone_code_hash = phone, cached
                return "already_sent"
            raise
        self._phone, self._phone_code_hash = phone, sent.phone_code_hash
        return "sent"

    async def sign_in_code(self, code: str) -> bool:
        """Returns True when logged in, False when a 2FA password is still needed."""
        from telethon.errors import SessionPasswordNeededError

        if not self.client or not self._phone:
            raise RuntimeError("צריך קודם לשלוח קוד")
        try:
            await self.client.sign_in(self._phone, code, phone_code_hash=self._phone_code_hash)
        except SessionPasswordNeededError:
            return False
        self.authorized = True
        return True

    async def sign_in_password(self, password: str) -> None:
        if not self.client:
            raise RuntimeError("צריך קודם לשלוח קוד")
        await self.client.sign_in(password=password)
        self.authorized = True

    async def log_out(self) -> None:
        if self.client and self.authorized:
            await self.client.log_out()
        self.authorized = False
        await self.start()

    async def _keep_connected(self) -> None:
        """Every 20 seconds: if the connection to Telegram was lost, connect again."""
        while True:
            await asyncio.sleep(20)
            try:
                await self._ensure()
            except Exception as e:  # noqa: BLE001 - try again next round
                log.warning("Telegram reconnect failed: %s", e)

    async def heal(self) -> None:
        """Downloads stopped answering although we look connected (happens after the network dropped:
        the extra connections used for files go stale). A fresh connection fixes it."""
        if not self.client or time.time() - getattr(self, "_healed_at", 0) < 60:
            return
        if time.time() - getattr(self, "_data_at", 0) < 20:  # other downloads are moving: not stuck
            return
        self._healed_at = time.time()
        log.warning("Telegram downloads are stuck - reconnecting")
        try:
            await asyncio.wait_for(self.client.disconnect(), 15)
        except Exception:  # noqa: BLE001
            pass
        self._msg_cache.clear()
        await self._ensure()

    async def _ensure(self) -> None:
        client = self.client
        if client and not client.is_connected():
            if not hasattr(self, "_connect_lock"):
                self._connect_lock = asyncio.Lock()
            async with self._connect_lock:
                if not client.is_connected():
                    log.info("Reconnecting to Telegram...")
                    await asyncio.wait_for(client.connect(), 30)
                    if not self.authorized:  # the site started while offline
                        self.authorized = await client.is_user_authorized()

    def _require(self) -> TelegramClient:
        if not self.client or not self.authorized:
            raise RuntimeError("Telegram is not connected / logged in")
        return self.client

    async def me(self) -> dict | None:
        if not self.authorized or not self.client:
            return None
        await self._ensure()
        u = await self.client.get_me()
        return {"name": " ".join(filter(None, [u.first_name, u.last_name])), "username": u.username}

    async def list_dialogs(self) -> list[dict]:
        await self._ensure()
        client = self._require()
        out = []
        async for d in client.iter_dialogs():
            if d.is_group or d.is_channel:
                out.append({"id": d.id, "title": d.name or str(d.id), "type": "channel" if d.is_channel and not d.is_group else "group"})
        return out

    async def _entity(self, chat_id: int):
        client = self._require()
        try:
            return await client.get_input_entity(chat_id)
        except ValueError:
            # Entity not in the session cache yet (e.g. a new login on another device): loading the
            # dialogs fills it. Done ONCE, even when many posters ask at the same moment.
            if not hasattr(self, "_dialogs_lock"):
                self._dialogs_lock = asyncio.Lock()
            async with self._dialogs_lock:
                try:
                    return await client.get_input_entity(chat_id)
                except ValueError:
                    await client.get_dialogs()
            return await client.get_input_entity(chat_id)

    async def iter_messages(self, chat_id: int, min_id: int = 0) -> AsyncIterator[tuple[int, VideoMessage | None]]:
        """Yield (message id, video or None) for every message newer than min_id, oldest first."""
        await self._ensure()
        client = self._require()
        entity = await self._entity(chat_id)
        # Telethon waits a whole second between pages of 100 messages unless told otherwise: on a channel with
        # 100,000 messages that alone is half an hour. A short pause is enough (Telegram's "slow down" is handled).
        async for msg in client.iter_messages(entity, min_id=min_id, reverse=True, wait_time=0.15):
            v = message_to_video(chat_id, msg)
            if v:
                pass  # not kept in memory: a big channel would fill the phone's memory
            elif getattr(msg, "file", None) is not None:
                v = NOT_VIDEO  # a file that is not a video (archive, picture, document...)
            yield msg.id, v

    async def get_message(self, chat_id: int, msg_id: int, fresh: bool = False):
        await self._ensure()
        key = (chat_id, msg_id)
        cached = self._msg_cache.get(key)
        if cached and not fresh and time.time() - cached[0] < MSG_CACHE_SECONDS:
            return cached[1]
        client = self._require()
        msg = await client.get_messages(await self._entity(chat_id), ids=msg_id)
        if msg is None:
            raise FileNotFoundError("message was deleted from Telegram")
        self._remember(key, msg)
        return msg

    async def stream(self, chat_id: int, msg_id: int, start: int, end: int) -> AsyncIterator[bytes]:
        """Yield bytes [start, end] (inclusive) of the file attached to the message."""
        client = self._require()
        aligned = start - (start % CHUNK)
        skip = start - aligned
        remaining = end - start + 1
        msg = await self.get_message(chat_id, msg_id)
        first_at: float | None = time.monotonic()
        for attempt in range(2):
            try:
                chunks = client.iter_download(msg.media, offset=aligned, request_size=CHUNK).__aiter__()
                while True:
                    try:  # a download that hangs (seen after the network dropped) -> reconnect and try again
                        chunk = await asyncio.wait_for(chunks.__anext__(), DOWNLOAD_STALL_SECONDS)
                    except StopAsyncIteration:
                        break
                    self._data_at = time.time()
                    if first_at is not None:
                        took = time.monotonic() - first_at
                        if took > 5:
                            log.warning("video: first data from Telegram took %.1f s", took)
                        first_at = None
                    if skip:
                        chunk = chunk[skip:]
                        skip = 0
                    if len(chunk) >= remaining:
                        yield bytes(chunk[:remaining])
                        return
                    remaining -= len(chunk)
                    aligned += CHUNK
                    yield bytes(chunk)
                return
            except FileReferenceExpiredError:
                if attempt:
                    raise
                msg = await self.get_message(chat_id, msg_id, fresh=True)
            except asyncio.TimeoutError:
                if attempt:
                    raise
                await self.heal()
                client = self._require()

    def _remember(self, key, msg) -> None:
        if len(self._msg_cache) > 3000:  # keep memory small: forget the oldest half
            for k in sorted(self._msg_cache, key=lambda k: self._msg_cache[k][0])[:1500]:
                self._msg_cache.pop(k, None)
        self._msg_cache[key] = (time.time(), msg)

    async def _get_message_batched(self, chat_id: int, msg_id: int):
        """A page shows dozens of pictures at once: ask Telegram for all their messages in ONE request
        instead of one request each (many quick requests make Telegram pause us for up to a minute)."""
        cached = self._msg_cache.get((chat_id, msg_id))
        if cached and time.time() - cached[0] < MSG_CACHE_SECONDS:
            return cached[1]
        if not hasattr(self, "_batch"):
            self._batch: dict[int, dict[int, asyncio.Future]] = {}
        waiting = self._batch.setdefault(chat_id, {})
        fut = waiting.get(msg_id)
        if fut is None:
            fut = waiting[msg_id] = asyncio.get_running_loop().create_future()
            if len(waiting) == 1:
                asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(self._flush_batch(chat_id)))
        return await asyncio.shield(fut)

    async def _flush_batch(self, chat_id: int) -> None:
        waiting = self._batch.pop(chat_id, {})
        ids = list(waiting)
        try:
            await self._ensure()
            client = self._require()
            entity = await self._entity(chat_id)
            for i in range(0, len(ids), 100):
                part = ids[i:i + 100]
                msgs = await client.get_messages(entity, ids=part)
                for mid, msg in zip(part, msgs):
                    if msg is not None:
                        self._remember((chat_id, mid), msg)
                        waiting[mid].set_result(msg)
                    else:
                        waiting[mid].set_exception(FileNotFoundError("message was deleted from Telegram"))
        except Exception as e:  # noqa: BLE001 - hand the error to everyone who is waiting
            for f in waiting.values():
                if not f.done():
                    f.set_exception(e)

    async def thumbnail(self, chat_id: int, msg_id: int) -> bytes | None:
        client = self._require()
        if not hasattr(self, "_thumb_sem"):  # a page of posters must not starve video streaming
            self._thumb_sem = asyncio.Semaphore(4)
        try:
            msg = await self._get_message_batched(chat_id, msg_id)
        except FileNotFoundError:
            return None
        async with self._thumb_sem:
            try:
                data = await client.download_media(msg, file=bytes, thumb=-1)
                self._data_at = time.time()
            except (TypeError, ValueError, IndexError):
                return None
        return data or None


class Scanner:
    """Walks enabled chats and adds new videos to the library."""

    def __init__(self, db, tg: TelegramService, tmdb, min_duration: int):
        from .library import add_video, regroup_all  # local import to avoid a cycle

        self._add_video = add_video
        self._regroup_all = regroup_all
        self.db = db
        self.tg = tg
        self.tmdb = tmdb
        self.min_duration = min_duration
        self.running = False
        self.status: dict = {"running": False, "chat": None, "added": 0, "last_finished": None, "error": None}
        self._lock = asyncio.Lock()

    def start_tmdb(self) -> None:
        if self.tmdb and not self.tmdb.status["running"]:
            self._tmdb_task = asyncio.create_task(self._tmdb_then_join())

    async def _tmdb_then_join(self) -> None:
        await self.tmdb.enrich_pending(self.db)
        from .library import merge_similar_movies  # names got cleaner: join movies that now match

        await asyncio.to_thread(merge_similar_movies, self.db)

    async def _scan_chat(self, chat: dict, rep: dict, last_seen: int) -> None:
        count = 0
        async for msg_id, v in self.tg.iter_messages(chat["id"], min_id=chat["last_msg_id"]):
            count += 1
            rep["checked"] = count
            last_seen = max(last_seen, msg_id)
            if v == NOT_VIDEO:
                rep["files"] += 1
                continue
            if v is None:
                continue
            rep["videos"] += 1
            if self._add_video(self.db, v, chat, self.min_duration):
                self.status["added"] += 1
                rep["added"] += 1
            elif self.min_duration and ((v.duration and v.duration < self.min_duration)
                                        or (not v.duration and v.size < 50 * 1024 * 1024)):
                rep["short"] += 1
            if count % 200 == 0:
                self.db.set_chat_scanned(chat["id"], last_seen)
        self.db.set_chat_scanned(chat["id"], last_seen)
        log.info("scanned chat %s: %d messages checked", chat["id"], count)

    async def scan(self, chat_ids: list[int] | None = None) -> dict:
        if self._lock.locked():
            self._again = True  # e.g. a group was just chosen while another scan runs: scan again right after
            return self.status
        self._again = False
        async with self._lock:
            self.status = {"running": True, "chat": None, "added": 0, "last_finished": self.status.get("last_finished"),
                           "error": None, "report": []}
            from .branding import VERSION as BRANDING_VERSION
            upgraded = self.db.get_pref("branding_version") != BRANDING_VERSION
            changed = upgraded or any(c.get("branding") is None for c in self.db.chats(enabled_only=True))
            changed_chats = {c["id"] for c in self.db.chats(enabled_only=True) if c.get("branding") is None}
            try:
                for chat in self.db.chats(enabled_only=True):
                    if chat_ids and chat["id"] not in chat_ids:
                        continue
                    self.status["chat"] = chat["title"]
                    last_seen = chat["last_msg_id"]
                    count = 0
                    # what was found in this group, so Settings can say why nothing was added
                    rep = {"chat": chat["title"], "checked": 0, "videos": 0, "added": 0, "short": 0,
                           "files": 0, "first": not chat["last_msg_id"]}
                    self.status["report"].append(rep)
                    try:
                        await self._scan_chat(chat, rep, last_seen)
                    except Exception as e:  # noqa: BLE001
                        if not is_no_access(e):
                            raise
                        # left / banned / deleted: say so in Settings and go on with the other groups
                        rep["no_access"] = True
                        rep["chat_id"] = chat["id"]
                        rep["library"] = self.db.q1("SELECT COUNT(*) AS n FROM media WHERE chat_id = ? AND hidden = 0",
                                                    (chat["id"],))["n"]
                        log.warning("no access to group %s: %s", chat["id"], type(e).__name__)
                        continue
                    count = rep["checked"]
                    if count:
                        changed = True
                        changed_chats.add(chat["id"])
                    if rep["added"]:
                        self.db.version += 1  # show this group's new videos right away
                if changed:  # learn channel signatures and re-file names (see branding.py)
                    self.status["chat"] = "מסדר שמות"
                    # only the groups with new videos (all of them after an update of the name rules)
                    await asyncio.to_thread(self._regroup_all, self.db, upgraded,
                                            None if upgraded or not changed_chats else changed_chats)
                    self.db.set_pref("branding_version", BRANDING_VERSION)
                if self.tmdb:  # posters are fetched in the background; scanning is not held up
                    self.start_tmdb()
            except Exception as e:  # noqa: BLE001 - surface any error in the UI
                log.exception("scan failed")
                self.status["error"] = str(e)
            finally:
                log.info("scan finished, %d new videos added", self.status["added"])
                self.status["running"] = False
                self.status["chat"] = None
                self.status["last_finished"] = time.time()
        if getattr(self, "_again", False):
            asyncio.ensure_future(self.scan())
        return self.status
