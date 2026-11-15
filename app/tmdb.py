"""Optional metadata (poster, overview, Hebrew name) from The Movie Database."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)
API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/"


class TMDB:
    def __init__(self, api_key: str):
        self.api_key = api_key
        # v4 "read access tokens" are JWTs, v3 keys are 32 hex chars
        self.bearer = api_key.startswith("eyJ")
        self._client: httpx.AsyncClient | None = None
        self.status = {"running": False, "done": 0, "total": 0}

    async def _get(self, client: httpx.AsyncClient, path: str, **params) -> dict:
        headers = {}
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["api_key"] = self.api_key
        for attempt in range(4):
            r = await client.get(API + path, params=params, headers=headers, timeout=15)
            if r.status_code == 429:  # rate limited: wait and retry
                await asyncio.sleep(float(r.headers.get("retry-after") or 1) + attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def _client_ctx(self):
        if self._client is not None:  # shared client during a bulk run
            client = self._client

            class _Shared:
                async def __aenter__(self_inner):
                    return client

                async def __aexit__(self_inner, *exc):
                    return False
            return _Shared()
        return httpx.AsyncClient()

    async def lookup(self, kind: str, name: str, year: int | None = None, tmdb_id: int | None = None) -> dict | None:
        media_type = "tv" if kind == "series" else "movie"
        async with self._client_ctx() as client:
            if tmdb_id:
                item = await self._get(client, f"/{media_type}/{tmdb_id}", language="he-IL")
            else:
                params = {"query": name, "language": "he-IL"}
                if year:
                    params["first_air_date_year" if media_type == "tv" else "year"] = year
                res = (await self._get(client, f"/search/{media_type}", **params)).get("results") or []
                if not res and year:
                    params.pop("first_air_date_year", None)
                    params.pop("year", None)
                    res = (await self._get(client, f"/search/{media_type}", **params)).get("results") or []
                if not res:
                    return None
                item = res[0]
            overview = item.get("overview")
            if not overview:
                en = await self._get(client, f"/{media_type}/{item['id']}", language="en-US")
                overview = en.get("overview")
        date = item.get("first_air_date") or item.get("release_date") or ""
        return {
            "tmdb_id": item["id"],
            "tmdb_name": item.get("name") or item.get("title"),
            "overview": overview,
            "poster": IMG + "w342" + item["poster_path"] if item.get("poster_path") else None,
            "backdrop": IMG + "w1280" + item["backdrop_path"] if item.get("backdrop_path") else None,
            "year": int(date[:4]) if date[:4].isdigit() else None,
        }

    async def enrich_title(self, db, title: dict, tmdb_id: int | None = None) -> bool:
        try:
            meta = await self.lookup(title["kind"], title["name"], title.get("year"), tmdb_id)
        except Exception as e:  # noqa: BLE001
            log.warning("TMDB lookup failed for %s: %s", title["name"], e)
            return False
        if not meta:
            db.update_title(title["id"], soft=True, tmdb_checked=1)
            return False
        db.update_title(
            title["id"],
            soft=True,
            tmdb_checked=1,
            tmdb_id=meta["tmdb_id"],
            name=meta["tmdb_name"] or title["name"],
            overview=meta["overview"],
            poster=meta["poster"],
            backdrop=meta["backdrop"],
            year=meta["year"] or title.get("year"),
        )
        # The same movie/series under another name (e.g. Hebrew vs English caption) -> one entry.
        # Always merge the newer entry into the older one so concurrent lookups agree.
        other = db.find_title_by_tmdb(title["kind"], meta["tmdb_id"], title["id"])
        if other:
            src, dst = max(other["id"], title["id"]), min(other["id"], title["id"])
            if db.get_title(src) and db.get_title(dst):
                db.merge_titles(src, dst)
        return True

    async def enrich_pending(self, db, concurrency: int = 6) -> None:
        if self.status["running"]:
            return
        pending = db.titles_needing_tmdb()
        self.status = {"running": True, "done": 0, "total": len(pending)}
        sem = asyncio.Semaphore(concurrency)

        async def one(title: dict) -> None:
            async with sem:
                if db.get_title(title["id"]):  # may have been merged meanwhile
                    await self.enrich_title(db, title)
                self.status["done"] += 1

        try:
            async with httpx.AsyncClient() as client:
                self._client = client
                for i in range(0, len(pending), 100):
                    await asyncio.gather(*(one(t) for t in pending[i:i + 100]))
        finally:
            self._client = None
            self.status["running"] = False
