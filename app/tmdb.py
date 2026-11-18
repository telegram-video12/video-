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

    async def _search_smart(self, client, media_type: str, name: str, duration: float | None) -> list:
        """Name has extra words that TMDB does not know ("אבא גנוב 1 גוזלן"): drop one or two words from the
        end or the start and search again. Accepted only when what is left is EXACTLY a TMDB name, and for a
        movie only when the file's length fits the movie's length - so different movies are never joined."""
        from .parser import normalize

        words = name.split()
        if len(words) < 2:
            return []
        n = len(words)
        # drop words from the end, from the start, then from both ("חן סרטים אבא גנוב 1 ישראלי")
        cuts = [(0, 1), (0, 2), (1, 0), (2, 0), (1, 1), (1, 2), (2, 1), (2, 2), (3, 0), (0, 3), (3, 1), (1, 3)]
        tried = set()
        for start, end in cuts:
            v = words[start:n - end]
            query = " ".join(v)
            if len(v) < 1 or len(normalize(query)) < 2 or query in tried:
                continue
            if any(ch.isdigit() for x in words[:start] + words[n - end:] for ch in x):
                continue  # never drop a number: "אבא גנוב 2" must not become "אבא גנוב"
            tried.add(query)
            res = (await self._get(client, f"/search/{media_type}", query=query, language="he-IL")).get("results") or []
            want = {normalize(query)}
            if len(v) > 1 and v[-1] == "1":  # the first movie is usually named without "1" on TMDB
                want.add(normalize(" ".join(v[:-1])))
            for item in res[:5]:
                names = {normalize(item.get("name") or item.get("title") or ""),
                         normalize(item.get("original_name") or item.get("original_title") or "")}
                if not want & names:
                    continue
                if media_type == "movie" and duration:
                    det = await self._get(client, f"/movie/{item['id']}")
                    runtime = (det.get("runtime") or 0) * 60
                    if runtime and abs(runtime - duration) > max(0.12 * runtime, 8 * 60):
                        continue  # same name, different length: probably another movie
                return [item]
        return []

    async def season(self, tmdb_id: int, season: int) -> list[dict]:
        """Episode names, short summaries and pictures of one season, in Hebrew (English summary when no Hebrew)."""
        async with self._client_ctx() as client:
            try:
                data = await self._get(client, f"/tv/{tmdb_id}/season/{season}", language="he-IL")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:  # TMDB has no such season (e.g. your channel's own numbering)
                    return []
                raise
            eps = data.get("episodes") or []
            if eps and not any(e.get("overview") for e in eps):
                try:
                    en = await self._get(client, f"/tv/{tmdb_id}/season/{season}", language="en-US")
                    by_num = {e.get("episode_number"): e for e in en.get("episodes") or []}
                    for e in eps:
                        e["overview"] = (by_num.get(e.get("episode_number")) or {}).get("overview")
                except Exception:  # noqa: BLE001 - names without summaries are fine too
                    pass
        out = []
        for e in eps:
            name = (e.get("name") or "").strip()
            if name and name.lower().replace(" ", "") in (f"episode{e.get('episode_number')}", f"פרק{e.get('episode_number')}"):
                name = ""  # "Episode 3" says nothing new
            out.append({"episode": e.get("episode_number"), "name": name or None, "overview": e.get("overview") or None,
                        "still": IMG + "w300" + e["still_path"] if e.get("still_path") else None})
        return [e for e in out if e["episode"] is not None]

    async def lookup(self, kind: str, name: str, year: int | None = None, tmdb_id: int | None = None,
                     have_overview: bool = False, duration: float | None = None, origin: str | None = None) -> dict | None:
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
                    res = await self._search_smart(client, media_type, name, duration)
                if not res:
                    return None
                # A Hebrew name: when TMDB also has an ISRAELI show/movie with exactly this name, take it before a
                # foreign one that only has this Hebrew translation ("הישרדות" = the Israeli show, not Survivor US).
                from .parser import normalize
                want = normalize(name)
                exact_he = [r for r in res[:10] if r.get("original_language") == "he"
                            and want in {normalize(r.get("name") or r.get("title") or ""),
                                         normalize(r.get("original_name") or r.get("original_title") or "")}]
                if origin == "il":  # the group/caption says Israeli: an Israeli result first
                    local = exact_he or [r for r in res[:5] if r.get("original_language") == "he"]
                    res = local + [r for r in res if r not in local]
                elif origin == "intl":  # from abroad: Israeli results last
                    res = [r for r in res if r.get("original_language") != "he"] + \
                          [r for r in res if r.get("original_language") == "he"]
                elif exact_he and any("\u0590" <= ch <= "\u05ff" for ch in name):
                    res = exact_he + [r for r in res if r not in exact_he]
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
        last = item.get("last_air_date") or date  # series: when the latest episode came out
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
            "last_year": int(last[:4]) if last[:4].isdigit() else None,
            "genres": [g["name"] for g in item.get("genres") or [] if g.get("name")],
            "rating": round(float(item["vote_average"]), 1) if item.get("vote_average") and votes >= 5 else None,
            "runtime": int(runtime) if runtime else None,
            "cast": cast,
            # movie: the movie series it belongs to (Spider-Man 1, 2, 3...); 0 = none
            "collection_id": ((item.get("belongs_to_collection") or {}).get("id") or 0) if media_type == "movie" else None,
        }

    async def enrich_title(self, db, title: dict, tmdb_id: int | None = None) -> bool:
        try:
            longest = db.q1("SELECT MAX(duration) AS d FROM media WHERE title_id = ?", (title["id"],))
            meta = await self.lookup(title["kind"], title["name"], title.get("year"), tmdb_id,
                                     have_overview=bool(title.get("overview")),
                                     duration=(longest or {}).get("d"), origin=title.get("origin"))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:  # key revoked/replaced: no point trying the rest
                if not self.stopped:
                    log.warning("TMDB rejected the API key (401) - stopping; set a valid key in Settings")
                self.stopped = True
            else:  # never log the URL: it contains the API key
                log.warning("TMDB lookup failed for %s: HTTP %s", title["name"], e.response.status_code)
            return False
        except httpx.TransportError:  # no internet right now (computer asleep, Wi-Fi dropped): the caller waits
            raise
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
            name=title["name"] if title.get("name_manual") else (meta["tmdb_name"] or title["name"]),
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
            last_year=meta.get("last_year"),
            collection_id=meta.get("collection_id"),
        )
        # The same movie/series under another name (e.g. Hebrew vs English caption) -> one entry.
        # Always merge the newer entry into the older one so concurrent lookups agree.
        other = db.find_title_by_tmdb(title["kind"], meta["tmdb_id"], title["id"])
        if not other and meta["tmdb_name"]:  # e.g. the extended cut has its own TMDB entry
            other = db.find_title_by_name_year(title["kind"], meta["tmdb_name"], meta["year"], title["id"])
        if other:
            src, dst = max(other["id"], title["id"]), min(other["id"], title["id"])
            if db.get_title(src) and db.get_title(dst):
                db.merge_titles(src, dst, soft=True)
        from .parser import normalize

        db.merge_by_name(normalize(meta["tmdb_name"] or ""), soft=True)  # entries whose name matches this TMDB name
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
                for attempt in range(60):  # without internet, wait for it to come back instead of skipping everything
                    if self.stopped or not db.get_title(title["id"]):  # may have been merged meanwhile
                        break
                    try:
                        await self.enrich_title(db, title, tmdb_id=title.get("tmdb_id"))
                        break
                    except httpx.TransportError:
                        if attempt == 0:
                            log.warning("TMDB not reachable (no internet?) - waiting and trying again")
                        self.status["waiting"] = True
                        await asyncio.sleep(10)
                self.status["waiting"] = False
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
