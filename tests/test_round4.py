"""Round 4: new episodes, genre rows, surprise me."""
import asyncio
import time

from test_api import FakeTG, client, login, scan, vm  # noqa: F401 - pytest fixture


def test_new_episodes_and_surprise(client):
    login(client)
    scan(client)
    lib = client.get("/api/library").json()["titles"]
    fauda = next(t for t in lib if t["name"] == "פאודה")
    ep1 = client.get(f"/api/titles/{fauda['id']}").json()["media"][0]
    client.post(f"/api/progress/{ep1['id']}", json={"position": 1450, "duration": 1500})  # finished ep 1
    assert client.get("/api/home").json()["new"] == []  # old episodes are not "new"

    # a new episode is posted after you watched
    tg = client.app.state.scanner.tg
    new = vm(20, "פאודה עונה 1 פרק 3")
    new.date = time.time() + 60
    tg.messages.append((20, new))
    asyncio.run(client.app.state.scanner.scan())
    home = client.get("/api/home").json()
    assert [t["name"] for t in home["new"]] == ["פאודה"] and home["new"][0]["new_count"] == 1
    detail = client.get(f"/api/titles/{fauda['id']}").json()
    ep3 = next(m for m in detail["media"] if m["episode"] == 3)
    assert ep3["added_at"] > detail["new_since"]  # gets the "new" badge on the title page

    # watching it clears the badge
    client.post(f"/api/progress/{ep3['id']}", json={"position": 1450, "duration": 1500})
    assert client.get("/api/home").json()["new"] == []

    # surprise me: never something you already started
    for _ in range(10):
        pick = client.get("/api/random").json()
        assert pick["id"] != fauda["id"]
    assert client.get("/api/random?kind=movie").json()["kind"] == "movie"


def test_genre_rows_follow_what_you_watch(client):
    login(client)
    db = client.app.state.db
    for i in range(12):
        tid = db.get_or_create_title("movie", f"סרט {i}", f"סרט{i}", 2000 + i)
        db.update_title(tid, genres='["אקשן"]' if i < 6 else '["קומדיה"]')
        db.add_media(chat_id=-100, msg_id=100 + i, title_id=tid, season=None, episode=None, file_name="", caption="",
                     size=1, mime="video/mp4", duration=6000, width=1, height=1, added_at=1000 + i)
    comedy = db.q1("SELECT id FROM media WHERE msg_id = 110")["id"]
    client.post(f"/api/progress/{comedy}", json={"position": 600, "duration": 6000})
    home = client.get("/api/home").json()
    assert home["personal"] and home["genres"][0]["genre"] == "קומדיה"
