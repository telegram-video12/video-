"""Same movie under a name with extra words: joined automatically or suggested."""
import asyncio

from test_api import wait_scans, FakeTG, client, login, scan, vm  # noqa: F401 - pytest fixture

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
    assert names == ["אבא גנוב", "אבא גנוב 2", "אבא גנוב 2 ישראלי"]  # "חן סרטים" is an uploader signature
    sugg = possible_duplicates(db)
    assert [(p["a"]["name"], p["b"]["name"], p["extras"]) for p in sugg] == \
        [("אבא גנוב 2", "אבא גנוב 2 ישראלי", ["ישראלי"])]


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
    wait_scans(client)
    sc.status["running"] = False
    asyncio.run(go())


def test_hebrew_name_prefers_the_israeli_show(monkeypatch):
    t = TMDB("k")

    async def fake_get(client, path, **params):
        if path.startswith("/search/tv"):
            return {"results": [{"id": 14658, "name": "הישרדות", "original_name": "Survivor", "original_language": "en"},
                                {"id": 42, "name": "הישרדות", "original_name": "הישרדות", "original_language": "he"}]}
        return {"id": int(path.split("/")[-1]), "name": "הישרדות", "genres": []}

    t._get = fake_get
    meta = asyncio.run(t.lookup("series", "הישרדות"))
    assert meta["tmdb_id"] == 42


def test_recheck_series_tied_to_a_foreign_show(client):
    from app.library import recheck_foreign_matches
    login(client)
    db = client.app.state.db
    a = db.get_or_create_title("series", "הישרדות", "הישרדות", None)
    db.update_title(a, tmdb_id=14658, tmdb_checked=1, tmdb_details=1, original_name="Survivor")
    b = db.get_or_create_title("series", "פאודה", "פאודה", None)
    db.update_title(b, tmdb_id=1, tmdb_checked=1, tmdb_details=1, original_name="פאודה")
    db.set_pref("tmdb_he_recheck", "")  # it already ran once when the site started (empty library then)
    recheck_foreign_matches(db)
    assert db.get_title(a)["tmdb_id"] is None and db.get_title(a)["tmdb_checked"] == 0
    assert db.get_title(b)["tmdb_id"] == 1  # an Israeli match stays


def test_israeli_and_foreign_show_with_the_same_name_stay_apart(client):
    login(client)
    tg = client.app.state.tg
    tg.messages = [
        (1, vm(1, "הישרדות עונה 1 פרק 1\nשודר ב: רשת 13")),       # Israeli hint
        (2, vm(2, "הישרדות עונה 1 פרק 1 מתורגם")),               # foreign hint
        (3, vm(3, "הישרדות עונה 1 פרק 2")),                       # no hint: joins
    ]
    scan(client)
    series = client.get("/api/library?kind=series").json()["titles"]
    assert sorted(t["name"] for t in series) == ["הישרדות", "הישרדות"]
    il = next(t for t in series if "ישראלי" in t["genres"])
    assert il["media_count"] == 2  # episodes 1 and 2 of the Israeli show; the translated one is its own show
    genres = [g["name"] for g in client.get("/api/genres?kind=series").json()["genres"]]
    assert "ישראלי" in genres


def test_group_marked_israeli(client):
    login(client)
    client.post("/api/chats", json={"chats": [{"id": -100, "title": "קבוצת סדרות", "enabled": True, "origin": "il"}]})
    assert client.get("/api/dialogs").json()[0]["origin"] == "il"
    client.app.state.scanner.status["running"] = False
    wait_scans(client)
    asyncio.run(client.app.state.scanner.scan())
    lib = client.get("/api/library?kind=series").json()["titles"]
    assert lib and all("ישראלי" in t["genres"] for t in lib)


def test_foreign_origin_puts_israeli_results_last():
    t = TMDB("k")

    async def fake_get(client, path, **params):
        if path.startswith("/search/tv"):
            return {"results": [{"id": 42, "name": "הישרדות", "original_name": "הישרדות", "original_language": "he"},
                                {"id": 14658, "name": "הישרדות", "original_name": "Survivor", "original_language": "en"}]}
        return {"id": int(path.split("/")[-1]), "name": "הישרדות", "genres": []}

    t._get = fake_get
    assert asyncio.run(t.lookup("series", "הישרדות", origin="intl"))["tmdb_id"] == 14658
    assert asyncio.run(t.lookup("series", "הישרדות", origin="il"))["tmdb_id"] == 42


def test_mixed_israeli_and_foreign_title_is_split(client):
    from app.library import split_mixed_titles
    login(client)
    db = client.app.state.db
    tid = db.get_or_create_title("series", "הישרדות", "הישרדות", None)
    db.update_title(tid, tmdb_id=32958, original_name="הישרדות")
    for i, cap in enumerate(["הישרדות עונה 8 פרק 1 שודר ב: רשת 13", "הישרדות עונה 8 פרק 2 מתורגם",
                             "הישרדות עונה 8 פרק 3 מתורגם", "הישרדות עונה 8 פרק 4"]):
        db.add_media(chat_id=-100, msg_id=700 + i, title_id=tid, season=8, episode=i + 1, file_name="", caption=cap,
                     size=1, mime="video/mp4", duration=1500, width=1, height=1, added_at=1000)
    assert split_mixed_titles(db) == 2
    names = {r["msg_id"]: r["title_id"] for r in db.q("SELECT msg_id, title_id FROM media")}
    assert names[700] == names[703] == tid and names[701] == names[702] != tid  # translated ones apart
    assert db.get_title(tid)["origin"] == "il" and split_mixed_titles(db) == 0  # once is enough


def test_split_by_channel_and_names_cleared_on_new_tmdb(client):
    login(client)
    db = client.app.state.db
    client.post("/api/chats", json={"chats": [{"id": -100, "title": "ישראלי", "enabled": True},
                                              {"id": -200, "title": "מתורגם", "enabled": True}]})
    tid = db.get_or_create_title("series", "הישרדות", "הישרדות", None)
    for i, chat in enumerate([-100, -200, -200]):
        db.add_media(chat_id=chat, msg_id=800 + i, title_id=tid, season=1, episode=i + 1, file_name="", caption="x",
                     size=1, mime="video/mp4", duration=1500, width=1, height=1, added_at=1000)
    chans = client.get(f"/api/titles/{tid}/channels").json()["channels"]
    assert [(c["title"], c["n"]) for c in chans] == [("מתורגם", 2), ("ישראלי", 1)]
    new = client.post(f"/api/titles/{tid}/split", json={"chat_id": -200}).json()["title_id"]
    assert new != tid and len(client.get(f"/api/titles/{new}").json()["media"]) == 2
    db.save_episode_info(tid, 1, [{"episode": 1, "name": "Old US name"}])
    db.update_title(tid, tmdb_id=32958)
    assert db.episode_info(tid) == {}


def test_duplicates_are_worked_out_in_the_background(tmp_path):
    import time as _t

    from app.db import Database
    from app.library import possible_duplicates

    db = Database(tmp_path / "d.db")
    for i, name in enumerate(("אבא גנוב", "אבא גנוב גוזלן")):
        tid = db.get_or_create_title("movie", name, name, 1987)
        db.add_media(chat_id=-1, msg_id=i, title_id=tid, season=None, episode=None, file_name="a.mp4", caption="",
                     size=1, mime="video/mp4", duration=5400, width=1, height=1, added_at=1)
    assert possible_duplicates(db, wait=False) is None  # Settings never waits: not known yet...
    for _ in range(100):
        if possible_duplicates(db, wait=False) is not None:
            break
        _t.sleep(0.02)
    pairs = possible_duplicates(db, wait=False)  # ...worked out in the background
    assert len(pairs) == 1
    db.set_pref("dup_dismissed", f'["{pairs[0]["key"]}"]')  # you dismissed it: worked out again at once
    assert possible_duplicates(db) == []
