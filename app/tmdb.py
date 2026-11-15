"""Optional metadata (poster, overview, Hebrew name) from The Movie Database."""
from __future__ import annotations

import asyncio
import json
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
        self.stopped = False  # set when the key is replaced or rejected: stop the background run

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

    async def _search_trimmed(self, client, media_type: str, name: str, year: int) -> list:
        """Last resort for names with leftover junk ("קינג סרט ספיידרמן 3"): drop a few words from the
        start or end and search again. Only accepted when the release year matches, to avoid wrong hits."""
        words = name.split()
        variants = [words[i:] for i in (1, 2, 3)] + [words[:-i] for i in (1, 2)]
        for v in variants:
            if not v or len("".join(v)) < 3 or not any(ch.isalpha() for ch in "".join(v)):
                continue
            params = {"query": " ".join(v), "language": "he-IL"}
            params["first_air_date_year" if media_type == "tv" else "year"] = year
            res = (await self._get(client, f"/search/{media_type}", **params)).get("results") or []
            for item in res:
                date = item.get("first_air_date") or item.get("release_date") or ""
                if date[:4].isdigit() and abs(int(date[:4]) - year) <= 1:
                    return [item]
        return []

    async def lookup(self, kind: str, name: str, year: int | None = None, tmdb_id: int | None = None,
                     have_overview: bool = False) -> dict | None:
        media_type = "tv" if kind == "series" else "movie"
        async with self._client_ctx() as client:
            if tmdb_id:  # already matched: ONE call brings details + cast
                item = await self._get(client, f"/{media_type}/{tmdb_id}", language="he-IL",
                                       append_to_response="credits")
            else:
                params = {"query": name, "language": "he-IL"}
                if year:
                    params["first_air_date_year" if media_type == "tv" else "year"] = year
                res = (await self._get(client, f"/search/{media_type}", **params)).get("results") or []
                if not res and year:
                    params.pop("first_air_date_year", None)
                    params.pop("year", None)
                    res = (await self._get(client, f"/search/{media_type}", **params)).get("results") or []
                if not res and year:
                    res = await self._search_trimmed(client, media_type, name, year)
                if not res:
                    return None
                item = res[0]
                # one details call gives genres, rating, length and cast (credits) in Hebrew
                det = await self._get(client, f"/{media_type}/{item['id']}", language="he-IL",
                                      append_to_response="credits")
                item = {**item, **det}
            overview = item.get("overview")
            if not overview and not have_overview:  # English overview only when there is none at all
                en = await self._get(client, f"/{media_type}/{item['id']}", language="en-US")
                overview = en.get("overview")
        date = item.get("first_air_date") or item.get("release_date") or ""
        runtime = item.get("runtime") or next(iter(item.get("episode_run_time") or []), None)
        cast = [{"name": c.get("name"), "photo": IMG + "w185" + c["profile_path"] if c.get("profile_path") else None}
                for c in ((item.get("credits") or {}).get("cast") or [])[:10] if c.get("name")]
        votes = item.get("vote_count") or 0
        return {
            "tmdb_id": item["id"],
            "tmdb_name": item.get("name") or item.get("title"),
            "original_name": item.get("original_name") or item.get("original_title"),
            "overview": overview,
            "poster": IMG + "w342" + item["poster_path"] if item.get("poster_path") else None,
            "backdrop": IMG + "w1280" + item["backdrop_path"] if item.get("backdrop_path") else None,
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "genres": [g["name"] for g in item.get("genres") or [] if g.get("name")],
            "rating": round(float(item["vote_average"]), 1) if item.get("vote_average") and votes >= 5 else None,
            "runtime": int(runtime) if runtime else None,
            "cast": cast,
        }

    async def enrich_title(self, db, title: dict, tmdb_id: int | None = None) -> bool:
        try:
            meta = await self.lookup(title["kind"], title["name"], title.get("year"), tmdb_id,
                                     have_overview=bool(title.get("overview")))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:  # key revoked/replaced: no point trying the rest
                if not self.stopped:
                    log.warning("TMDB rejected the API key (401) - stopping; set a valid key in Settings")
                self.stopped = True
            else:  # never log the URL: it contains the API key
                log.warning("TMDB lookup failed for %s: HTTP %s", title["name"], e.response.status_code)
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("TMDB lookup failed for %s: %s", title["name"], type(e).__name__)
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
            overview=meta["overview"] or title.get("overview"),
            poster=meta["poster"],
            backdrop=meta["backdrop"],
            year=meta["year"] or title.get("year"),
            original_name=meta.get("original_name"),
            genres=json.dumps(meta.get("genres") or [], ensure_ascii=False),
            rating=meta.get("rating"),
            runtime=meta.get("runtime"),
            cast_json=json.dumps(meta.get("cast") or [], ensure_ascii=False),
            tmdb_details=1,
        )
        # The same movie/series under another name (e.g. Hebrew vs English caption) -> one entry.
        # Always merge the newer entry into the older one so concurrent lookups agree.
        other = db.find_title_by_tmdb(title["kind"], meta["tmdb_id"], title["id"])
        if not other and meta["tmdb_name"]:  # e.g. the extended cut has its own TMDB entry
            other = db.find_title_by_name_year(title["kind"], meta["tmdb_name"], meta["year"], title["id"])
        if other:
            src, dst = max(other["id"], title["id"]), min(other["id"], title["id"])
            if db.get_title(src) and db.get_title(dst):
                db.merge_titles(src, dst)
        from .parser import normalize

        db.merge_by_name(normalize(meta["tmdb_name"] or ""))  # entries whose name matches this TMDB name
        return True

    async def enrich_pending(self, db, concurrency: int = 12) -> None:
        if self.status["running"]:
            return
        pending = db.titles_needing_tmdb()
        self.status = {"running": True, "done": 0, "total": len(pending)}
        sem = asyncio.Semaphore(concurrency)

        async def one(title: dict) -> None:
            async with sem:
                if self.stopped:
                    return
                if db.get_title(title["id"]):  # may have been merged meanwhile
                    await self.enrich_title(db, title, tmdb_id=title.get("tmdb_id"))
                self.status["done"] += 1

        try:
            async with httpx.AsyncClient() as client:
                self._client = client
                for i in range(0, len(pending), 100):
                    if self.stopped:
                        break
                    await asyncio.gather(*(one(t) for t in pending[i:i + 100]))
        finally:
            self._client = None
            self.status["running"] = False
