"""Same movie under a name with extra words: joined automatically or suggested."""
import asyncio

from test_api import FakeTG, client, login, scan, vm  # noqa: F401 - pytest fixture

from app.tmdb import TMDB


def test_extra_word_movies_join_and_suggestions(client):
    login(client)
    tg = client.app.state.tg
    tg.messages = [
        (1, vm(1, "אבא גנוב 1", duration=5400)), (2, vm(2, "אבא גנוב 1 גוזלן", duration=5410)),
        (3, vm(3, "אבא גנוב 1 BR", duration=5400)),
        (4, vm(4, "אבא גנוב 2", duration=5400)),                     # a number: another movie
        (5, vm(5, "המסע", duration=6000)), (6, vm(6, "המסע הגדול", duration=7200)),  # other length: only suggested
        (7, vm(7, "עובדה פרק 1")), (8, vm(8, "עובדה רשת פרק 1")),        # series: only suggested
    ]
    scan(client)
    lib = client.get("/api/library?limit=50").json()["titles"]
    names = sorted(t["name"] for t in lib)
    assert names.count("אבא גנוב 1") == 1 and "אבא גנוב 1 גוזלן" not in names and "אבא גנוב 2" in names
    abba = next(t for t in lib if t["name"] == "אבא גנוב 1")
    detail = client.get(f"/api/titles/{abba['id']}").json()
    assert detail["media"][0]["versions"] == 3  # all three uploads kept inside as versions

    pairs = client.get("/api/duplicates").json()["pairs"]
    got = sorted((p["a"]["name"], p["b"]["name"], p["extra"]) for p in pairs)
    assert got == [("המסע", "המסע הגדול", "הגדול"), ("עובדה", "עובדה רשת", "רשת")]
    assert client.get("/api/status").json()["duplicates"] == 2

    # "not the same" hides the suggestion
    client.post("/api/duplicates/dismiss", json={"key": pairs[0]["key"]})
    assert len(client.get("/api/duplicates").json()["pairs"]) == 1


def test_smart_tmdb_search_drops_extra_words_only_on_exact_match():
    t = TMDB("k")
    calls = []

    async def fake_get(client, path, **params):
        calls.append((path, params.get("query")))
        if path.startswith("/search/movie"):
            if params["query"] == "אבא גנוב 1":
                return {"results": [{"id": 7, "title": "אבא גנוב 1", "original_title": "Abba Ganuv"}]}
            if params["query"] == "הנוקמים":
                return {"results": [{"id": 9, "title": "הנוקמים"}]}
            return {"results": []}
        if path == "/movie/7":
            return {"runtime": 90}
        if path == "/movie/9":
            return {"runtime": 143}
        return {}

    t._get = fake_get
    run = lambda name, d: asyncio.run(t._search_smart(None, "movie", name, d))  # noqa: E731
    assert run("אבא גנוב 1 גוזלן", 5400)[0]["id"] == 7
    assert run("אבא גנוב 1 גוזלן", 3000) == []            # length does not fit: not joined
    assert run("גוזלן אבא גנוב 1", 5400)[0]["id"] == 7      # extra word at the start
    assert run("אבא גנוב", 5400) == []                     # nothing left that matches exactly


def test_smart_search_both_sides_and_first_movie_without_1():
    t = TMDB("k")

    async def fake_get(client, path, **params):
        if path.startswith("/search/movie"):
            q = params["query"]
            if q in ("אבא גנוב 1", "אבא גנוב"):
                return {"results": [{"id": 7, "title": "אבא גנוב"}]}
            if q == "אבא גנוב 2":
                return {"results": [{"id": 8, "title": "אבא גנוב 2"}]}
            return {"results": []}
        return {"runtime": 90}

    t._get = fake_get
    run = lambda name: asyncio.run(t._search_smart(None, "movie", name, 5400))  # noqa: E731
    assert run("אלי ה סרטים אבא גנוב 1 ישראלי")[0]["id"] == 7   # 3 words in front, 1 after
    assert run("סרטים אבא גנוב 1 ישראלי")[0]["id"] == 7         # one word each side, "1" = no number
    assert run("חן סרטים אבא גנוב 2 ישראלי הועלה")[0]["id"] == 8
    assert run("אבא גנוב 2 גוזלן")[0]["id"] == 8                 # never "אבא גנוב" (the 2 is kept)


def test_name_inside_a_longer_name_joins_same_movie(client):
    login(client)
    db = client.app.state.db
    tg = client.app.state.tg
    tg.messages = [
        (1, vm(1, "אבא גנוב (1987)", duration=5220)), (2, vm(2, "אבא גנוב 2 (1989)", duration=5400)),
        (3, vm(3, "אלי ה סרטים אבא גנוב 1 ישראלי", duration=5230)),
        (4, vm(4, "חן סרטים אבא גנוב 2 ישראלי הועלה", duration=5410)),
        (5, vm(5, "אבא גנוב 1 - טבריה", duration=5220)),
        (6, vm(6, "חן סרטים אבא גנוב 2 ישראלי", duration=6600)),     # other length: only suggested
    ]
    scan(client)
    # pretend TMDB recognised the two clean names
    for name, tid in (("אבא גנוב", 7), ("אבא גנוב 2", 8)):
        db.x("UPDATE titles SET tmdb_id = ?, tmdb_checked = 1 WHERE name = ?", (tid, name))
    from app.library import merge_similar_movies, possible_duplicates
    merge_similar_movies(db)
    db._titles_cache = None
    names = sorted(t["name"] for t in client.get("/api/library?limit=50").json()["titles"])
    assert names == ["אבא גנוב", "אבא גנוב 2", "חן סרטים אבא גנוב 2 ישראלי"]
    sugg = possible_duplicates(db)
    assert [(p["a"]["name"], p["b"]["name"], p["extras"]) for p in sugg] == \
        [("אבא גנוב 2", "חן סרטים אבא גנוב 2 ישראלי", ["חן סרטים", "ישראלי"])]


def test_scan_report_says_why_nothing_was_added(client):
    login(client)
    tg = client.app.state.tg
    tg.messages = [(1, "not-video"), (2, "not-video"), (3, None), (4, vm(4, "טריילר", duration=60))]
    scan(client)
    rep = client.get("/api/status").json()["scan"]["report"][0]
    assert (rep["checked"], rep["files"], rep["videos"], rep["short"], rep["added"]) == (4, 2, 1, 1, 0)


def test_scan_asked_while_busy_runs_again_after(client):
    import asyncio
    login(client)
    sc = client.app.state.scanner

    async def go():
        async with sc._lock:  # another scan is running
            await sc.scan()
        assert sc._again is True
        await asyncio.sleep(0.05)  # ... and the waiting one runs once the lock is free
    client.post("/api/chats", json={"chats": [{"id": -100, "title": "קבוצת סדרות", "enabled": True}]})
    sc.status["running"] = False
    asyncio.run(go())
