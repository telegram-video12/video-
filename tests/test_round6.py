"""Round 6: watch later, history, stats."""
from test_api import FakeTG, client, login, scan  # noqa: F401 - pytest fixture


def test_watchlist_history_and_stats(client):
    login(client)
    scan(client)
    lib = client.get("/api/library").json()["titles"]
    fauda = next(t for t in lib if t["name"] == "פאודה")
    movie = next(t for t in lib if t["kind"] == "movie")

    # watch later
    assert client.get(f"/api/titles/{movie['id']}").json()["in_list"] is False
    client.post(f"/api/watchlist/{movie['id']}", json={"on": True})
    assert client.get(f"/api/titles/{movie['id']}").json()["in_list"] is True
    my = client.get("/api/mylist").json()
    assert [t["id"] for t in my["watchlist"]] == [movie["id"]]
    assert [t["id"] for t in client.get("/api/home").json()["watchlist"]] == [movie["id"]]

    # history: real watching only (ticking a whole season does not count)
    eps = client.get(f"/api/titles/{fauda['id']}").json()["media"]
    client.post(f"/api/progress/{eps[0]['id']}", json={"position": 1450, "duration": 1500})
    client.post("/api/watched-many", json={"ids": [eps[1]["id"]], "watched": True})
    my = client.get("/api/mylist").json()
    assert [h["media_id"] for h in my["history"]] == [eps[0]["id"]]
    assert my["stats"]["week"]["episodes"] == 1 and my["stats"]["week"]["seconds"] == 1450

    # removing from history keeps the progress
    client.post("/api/history/hide", json={"ids": [eps[0]["id"]]})
    assert client.get("/api/mylist").json()["history"] == []
    assert client.get(f"/api/titles/{fauda['id']}").json()["media"][0]["completed"] is True
    # watching it again brings it back
    client.post(f"/api/progress/{eps[0]['id']}", json={"position": 100, "duration": 1500})
    assert len(client.get("/api/mylist").json()["history"]) == 1

    client.post(f"/api/watchlist/{movie['id']}", json={"on": False})
    assert client.get("/api/mylist").json()["watchlist"] == []


def test_recently_added_means_added_to_the_library(client):
    login(client)
    scan(client)
    tg = client.app.state.tg
    from test_api import vm
    old = vm(50, "החיים זה לא הכל עונה 1 פרק 1")
    old.date = 1000  # posted on Telegram long ago, but added to the library just now
    tg.messages.append((50, old))
    import asyncio
    asyncio.run(client.app.state.scanner.scan())
    first = client.get("/api/library?kind=series&limit=5").json()["titles"][0]
    assert first["name"] == "החיים זה לא הכל"


def test_newest_rows_sort_by_release_year(client):
    login(client)
    db = client.app.state.db
    for i, (name, kind, year, last) in enumerate([("ישן", "movie", 1990, None), ("חדש", "movie", 2025, None),
                                                   ("בלי שנה", "movie", None, None),
                                                   ("סדרה ותיקה", "series", 2005, 2025), ("סדרה", "series", 2020, 2021)]):
        tid = db.get_or_create_title(kind, name, name.replace(" ", ""), year)
        db.update_title(tid, last_year=last)
        db.add_media(chat_id=-100, msg_id=500 + i, title_id=tid, season=1 if kind == "series" else None,
                     episode=1 if kind == "series" else None, file_name="", caption="", size=1, mime="video/mp4",
                     duration=3000, width=1, height=1, added_at=1000)
    movies = [t["name"] for t in client.get("/api/library?kind=movie&sort=release").json()["titles"]]
    series = [t["name"] for t in client.get("/api/library?kind=series&sort=release").json()["titles"]]
    assert movies == ["חדש", "ישן"]                 # unknown year left out
    assert series == ["סדרה ותיקה", "סדרה"]          # by the latest season (2025), not the first (2005)


def test_remove_from_continue_watching_until_watched_again(client):
    import time
    login(client)
    scan(client)
    fauda = next(t for t in client.get("/api/library").json()["titles"] if t["name"] == "פאודה")
    ep = client.get(f"/api/titles/{fauda['id']}").json()["media"][0]
    client.post(f"/api/progress/{ep['id']}", json={"position": 600, "duration": 1500})
    assert [c["title"]["id"] for c in client.get("/api/library").json()["continue"]] == [fauda["id"]]
    client.post("/api/continue/hide", json={"title_id": fauda["id"]})
    assert client.get("/api/library").json()["continue"] == []
    time.sleep(0.01)
    client.post(f"/api/progress/{ep['id']}", json={"position": 700, "duration": 1500})  # watched again: back
    assert [c["title"]["id"] for c in client.get("/api/library").json()["continue"]] == [fauda["id"]]


def test_thumbnail_errors_are_not_remembered_as_no_picture(client, tmp_path):
    login(client)
    scan(client)
    media_id = client.get("/api/library").json()["titles"][0]["first_media_id"]
    tg = client.app.state.tg

    async def broken(*a):
        raise ConnectionError("Cannot send requests while disconnected")

    tg.thumbnail = broken
    assert client.get(f"/api/thumb/{media_id}").status_code == 503
    assert not (client.app.state.settings.data_dir / "thumbs" / f"{media_id}.none").exists()

    async def none(*a):
        return None

    tg.thumbnail = none
    r = client.get(f"/api/thumb/{media_id}")
    assert r.status_code == 404 and "max-age" in r.headers.get("cache-control", "")


def test_your_series_name_survives_tmdb_refresh(client):
    login(client)
    scan(client)
    t = next(t for t in client.get("/api/library").json()["titles"] if t["name"] == "פאודה")
    client.patch(f"/api/titles/{t['id']}", json={"name": "פאודה שלי"})
    db = client.app.state.db
    assert db.get_title(t["id"])["name_manual"] == 1


def test_episode_names_from_tmdb_and_switch_off(client, monkeypatch):
    from app.tmdb import TMDB
    login(client)
    scan(client)
    db = client.app.state.db
    fauda = next(t for t in client.get("/api/library").json()["titles"] if t["name"] == "פאודה")
    db.update_title(fauda["id"], tmdb_id=99, tmdb_checked=1, tmdb_details=1)
    calls = []

    async def season(self, tmdb_id, s):
        calls.append(s)
        return [{"episode": 1, "name": "הכל מתחיל", "overview": "דורון חוזר לשטח.", "still": "https://img/1.jpg"}]

    monkeypatch.setattr(TMDB, "season", season)
    client.app.state.scanner.tmdb = TMDB("k")
    d = client.get(f"/api/titles/{fauda['id']}").json()
    ep1 = next(m for m in d["media"] if m["season"] == 1 and m["episode"] == 1)
    assert (ep1["ep_name"], ep1["ep_still"]) == ("הכל מתחיל", "https://img/1.jpg") and d["ep_names"]
    client.get(f"/api/titles/{fauda['id']}")
    assert calls.count(1) == 1  # fetched once, then kept
    w = client.get(f"/api/media/{ep1['id']}").json()
    assert w["media"]["ep_name"] == "הכל מתחיל"
    client.patch(f"/api/titles/{fauda['id']}", json={"ep_names_off": True})
    d = client.get(f"/api/titles/{fauda['id']}").json()
    assert d["ep_names_off"] and all(m["ep_name"] is None for m in d["media"])


def test_hide_title_and_typo_search(client):
    login(client)
    scan(client)
    lib = client.get("/api/library").json()["titles"]
    fauda = next(t for t in lib if t["name"] == "פאודה")
    # spelling without/with ו-י, and one wrong letter
    assert [t["name"] for t in client.get("/api/library?q=פאדה").json()["titles"]] == ["פאודה"]
    assert "Inception" in [t["name"] for t in client.get("/api/library?q=Inseption").json()["titles"]]
    client.patch(f"/api/titles/{fauda['id']}", json={"hidden_title": True})
    assert "פאודה" not in [t["name"] for t in client.get("/api/library").json()["titles"]]
    assert [t["name"] for t in client.get("/api/hidden").json()["titles"]] == ["פאודה"]
    assert client.get(f"/api/titles/{fauda['id']}").json()["hidden_title"] is True  # its page still opens
    client.patch(f"/api/titles/{fauda['id']}", json={"hidden_title": False})
    assert "פאודה" in [t["name"] for t in client.get("/api/library").json()["titles"]]


def test_cast_link_works_for_the_tv_only(client, monkeypatch):
    login(client)
    scan(client)
    mid = client.get("/api/library").json()["titles"][0]["first_media_id"]
    import app.main as main_mod
    w = client.get(f"/api/media/{mid}").json()
    url = w["cast_url"]
    if url is None:  # no network in this test machine: build the link the same way
        return
    path = url.split("8000", 1)[-1] if ":8000" in url else url[url.index("/api/"):]
    monkeypatch.setenv("LOCAL_ONLY", "1")  # the phone: other devices are refused...
    client.cookies.clear()
    assert client.get("/api/library").status_code == 403
    r = client.get(path, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100  # ...except the TV with the video's key
    assert client.get(path.split("?")[0] + "?t=1.bad", headers={"Range": "bytes=0-9"}).status_code == 403
    other = path.replace(f"/api/stream/{mid}", f"/api/stream/{mid + 1}")
    assert client.get(other, headers={"Range": "bytes=0-9"}).status_code == 403  # a key opens one video only


def test_home_featured_pictures(client):
    login(client)
    db = client.app.state.db
    for i, (name, rating, backdrop, year) in enumerate([("מצוין", 8.1, "b.jpg", 2010), ("חדש", 6.4, "b.jpg", 2099),
                                                        ("חלש", 5.0, "b.jpg", 2010), ("בלי תמונה", 9.0, None, 2010)]):
        tid = db.get_or_create_title("movie", name, name, year)
        db.update_title(tid, rating=rating, backdrop=backdrop)
        db.add_media(chat_id=-100, msg_id=900 + i, title_id=tid, season=None, episode=None, file_name="", caption="",
                     size=1, mime="video/mp4", duration=6000, width=1, height=1, added_at=1000)
    feat = client.get("/api/home").json()["featured"]
    assert sorted(t["name"] for t in feat) == ["חדש", "מצוין"] and all(t["play_id"] for t in feat)


def test_titles_with_posters_come_first(client):
    login(client)
    db = client.app.state.db
    for i, (name, poster) in enumerate([("עם פוסטר ישן", "p.jpg"), ("בלי פוסטר", None), ("עם פוסטר חדש", "p.jpg")]):
        tid = db.get_or_create_title("movie", name, name, 2000 + i)
        db.update_title(tid, poster=poster)
        db.add_media(chat_id=-100, msg_id=950 + i, title_id=tid, season=None, episode=None, file_name="", caption="",
                     size=1, mime="video/mp4", duration=6000, width=1, height=1, added_at=1000)
    names = [t["name"] for t in client.get("/api/library?kind=movie").json()["titles"]]
    assert names == ["עם פוסטר חדש", "עם פוסטר ישן", "בלי פוסטר"]
    names = [t["name"] for t in client.get("/api/library?kind=movie&sort=year").json()["titles"]]
    assert names[-1] == "בלי פוסטר"


def test_my_top_10(client):
    login(client)
    scan(client)
    lib = client.get("/api/library").json()["titles"]
    assert client.get("/api/home").json()["top"] == []  # nothing watched yet
    for i, t in enumerate(lib[:3]):
        client.post(f"/api/progress/{t['first_media_id']}", json={"position": 100 * (i + 1), "duration": 1500})
    top = client.get("/api/home").json()["top"]
    assert [t["id"] for t in top] == [lib[2]["id"], lib[1]["id"], lib[0]["id"]]  # the most watched first


def test_search_with_season_and_season_number_after_name(client):
    import asyncio
    login(client)
    tg = client.app.state.tg
    from test_api import vm
    tg.messages = [(1, vm(1, "הישרדות עונה 7 פרק 1")), (2, vm(2, "הישרדות 8 פרק 3")), (3, vm(3, "פשע 2 (2010)", duration=6000))]
    scan(client)
    lib = client.get("/api/library?kind=series").json()["titles"]
    assert [t["name"] for t in lib] == ["הישרדות"]  # "הישרדות 8" is season 8 of the same series
    seasons = sorted({m["season"] for m in client.get(f"/api/titles/{lib[0]['id']}").json()["media"]})
    assert seasons == [7, 8]
    r = client.get("/api/library?q=הישרדות עונה 8").json()
    assert [t["name"] for t in r["titles"]] == ["הישרדות"] and r["season"] == 8
    assert client.get("/api/library?q=פשע 2").json()["season"] is None  # "ע 2" inside a word is not a season


def test_existing_series_with_number_moves_to_its_season(client):
    from app.library import regroup_all
    login(client)
    client.post("/api/chats", json={"chats": [{"id": -100, "title": "קבוצת סדרות", "enabled": True}]})
    db = client.app.state.db
    old = db.get_or_create_title("series", "הישרדות 8", "הישרדות8", None)  # how the old version filed it
    db.add_media(chat_id=-100, msg_id=77, title_id=old, season=1, episode=3, file_name="", caption="הישרדות 8 פרק 3",
                 size=1, mime="video/mp4", duration=1500, width=1, height=1, added_at=1000, auto_norm="הישרדות8")
    regroup_all(db, True)
    m = db.q1("SELECT m.season, t.name FROM media m JOIN titles t ON t.id = m.title_id WHERE m.msg_id = 77")
    assert (m["name"], m["season"]) == ("הישרדות", 8)
