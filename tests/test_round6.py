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
