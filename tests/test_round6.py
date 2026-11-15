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
