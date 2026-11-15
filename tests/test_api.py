"""End-to-end API tests with a fake Telegram backend (no network)."""
import os
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.telegram import VideoMessage

FILE = bytes(range(256)) * 12000  # ~3MB fake video


def vm(msg_id, caption, name="", duration=1500.0, size=len(FILE)):
    return VideoMessage(chat_id=-100, msg_id=msg_id, caption=caption, file_name=name, size=size,
                        mime="video/mp4", duration=duration, width=1920, height=1080, date=1000 + msg_id)


MESSAGES = [
    (1, vm(1, "פאודה עונה 1 פרק 1")),
    (2, None),  # text message
    (3, vm(3, "פאודה עונה 1 פרק 2")),
    (4, vm(4, "", "Fauda.S02E01.720p.mkv")),
    (5, vm(5, "פרק 1")),  # no name -> chat title
    (6, vm(6, "Inception (2010) 1080p")),
    (7, vm(7, "טריילר", duration=60)),  # too short, ignored
]


class FakeTG:
    configured = True
    authorized = True
    client = None

    def __init__(self):
        self.messages = list(MESSAGES)

    async def start(self, api_id=None, api_hash=None):
        self.started_with = (api_id, api_hash)

    async def send_code(self, phone):
        self.phone = phone

    async def sign_in_code(self, code):
        if code != "12345":
            raise type("PhoneCodeInvalidError", (Exception,), {})()
        return False  # needs 2FA

    async def sign_in_password(self, password):
        self.authorized = True

    async def log_out(self):
        self.authorized = False

    async def stop(self): ...
    async def me(self): return {"name": "Test", "username": "t"}

    async def list_dialogs(self):
        return [{"id": -100, "title": "קבוצת סדרות", "type": "group"}]

    async def iter_messages(self, chat_id, min_id=0):
        for mid, v in self.messages:
            if mid > min_id:
                yield mid, v

    async def get_message(self, chat_id, msg_id, fresh=False):
        return SimpleNamespace(id=msg_id)

    async def stream(self, chat_id, msg_id, start, end):
        # deliver in uneven chunks like the real thing
        pos = start
        while pos <= end:
            n = min(700_000, end - pos + 1)
            yield FILE[pos:pos + n]
            pos += n

    async def thumbnail(self, chat_id, msg_id):
        return None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SITE_PASSWORD", "")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("SCAN_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("MIN_DURATION_SECONDS", "180")
    app = create_app(Settings(), tg=FakeTG())
    with TestClient(app) as c:
        yield c


def login(c):
    if c.get("/api/setup-state").json()["needs_setup"]:
        assert c.post("/api/setup", json={"password": "secret"}).status_code == 200
        c.cookies.clear()
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    assert c.post("/api/login", json={"password": "secret"}).status_code == 200


def scan(c):
    c.post("/api/chats", json={"chats": [{"id": -100, "title": "קבוצת סדרות", "enabled": True}]})
    c.app.state.scanner.status["running"] = False
    import asyncio
    asyncio.run(c.app.state.scanner.scan())


def test_setup_then_login(client):
    assert client.get("/api/setup-state").json() == {"needs_setup": True}
    assert client.get("/api/library").status_code == 401
    assert client.post("/api/setup", json={"password": "ab"}).status_code == 400
    assert client.post("/api/setup", json={"password": "secret"}).status_code == 200
    assert client.get("/api/library").status_code == 200  # setup logs you in
    assert client.post("/api/setup", json={"password": "other"}).status_code == 403
    client.post("/api/password", json={"current": "secret", "new": "newpass"})
    client.cookies.clear()
    assert client.post("/api/login", json={"password": "secret"}).status_code == 401
    assert client.post("/api/login", json={"password": "newpass"}).status_code == 200


def test_telegram_web_login(client):
    login(client)
    tg = client.app.state.tg
    tg.authorized = False
    r = client.post("/api/settings", json={"api_id": "123", "api_hash": "xyz"})
    assert r.status_code == 400
    r = client.post("/api/settings", json={"api_id": "123", "api_hash": "a" * 32})
    assert r.status_code == 200 and tg.started_with == (123, "a" * 32)
    assert client.app.state.settings.api_id == 123
    assert client.post("/api/tg/send-code", json={"phone": "050-123 4567"}).json()["phone"] == "+972501234567"
    assert client.post("/api/tg/sign-in", json={"code": "11111"}).status_code == 400
    assert client.post("/api/tg/sign-in", json={"code": "12345"}).json()["need_password"] is True
    assert client.post("/api/tg/sign-in", json={"password": "pw"}).json()["done"] is True
    assert client.get("/api/status").json()["telegram_authorized"] is True
    # settings persisted to config.json
    from app.config import Settings
    assert Settings().api_hash == "a" * 32


def test_requires_login(client):
    login(client)
    client.cookies.clear()
    assert client.get("/api/library").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/login"
    assert client.get("/login").status_code == 200


def test_scan_builds_library(client):
    login(client)
    scan(client)
    lib = client.get("/api/library").json()
    names = {t["name"]: t for t in lib["titles"]}
    assert set(names) == {"פאודה", "Fauda", "קבוצת סדרות", "Inception"}
    assert names["פאודה"]["media_count"] == 2
    assert names["Inception"]["kind"] == "movie"
    # rescanning does not duplicate
    client.app.state.db.reset_chat(-100)
    scan(client)
    assert client.get("/api/library").json()["titles"][0]["media_count"] >= 1
    assert sum(t["media_count"] for t in client.get("/api/library").json()["titles"]) == 5


def test_progress_and_continue_watching(client):
    login(client)
    scan(client)
    lib = client.get("/api/library").json()
    fauda = next(t for t in lib["titles"] if t["name"] == "פאודה")
    detail = client.get(f"/api/titles/{fauda['id']}").json()
    ep1, ep2 = detail["media"]
    assert (ep1["season"], ep1["episode"], ep2["episode"]) == (1, 1, 2)
    assert detail["resume"]["state"] == "start"

    # stop in the middle of episode 1
    client.post(f"/api/progress/{ep1['id']}", json={"position": 600, "duration": 1500})
    cont = client.get("/api/library").json()["continue"]
    assert cont[0]["media"]["id"] == ep1["id"] and cont[0]["position"] == 600 and cont[0]["state"] == "resume"

    # finish episode 1 -> continue points to episode 2
    r = client.post(f"/api/progress/{ep1['id']}", json={"position": 1450, "duration": 1500}).json()
    assert r["completed"] is True
    cont = client.get("/api/library").json()["continue"]
    assert cont[0]["media"]["id"] == ep2["id"] and cont[0]["state"] == "next"

    # neighbours
    m = client.get(f"/api/media/{ep1['id']}").json()
    assert m["next"]["id"] == ep2["id"] and m["prev"] is None

    # mark episode 2 watched -> series done, disappears from continue
    client.post(f"/api/watched/{ep2['id']}", json={"watched": True})
    assert all(c["title"]["id"] != fauda["id"] for c in client.get("/api/library").json()["continue"])
    client.post(f"/api/watched/{ep2['id']}", json={"watched": False})
    assert client.get(f"/api/media/{ep2['id']}").json()["media"]["completed"] is False


def test_edit_and_merge(client):
    login(client)
    scan(client)
    titles = {t["name"]: t for t in client.get("/api/library").json()["titles"]}
    # merge "Fauda" (season 2 from file name) into "פאודה"
    client.post(f"/api/titles/{titles['Fauda']['id']}/merge", json={"into": titles["פאודה"]["id"]})
    detail = client.get(f"/api/titles/{titles['פאודה']['id']}").json()
    assert [(m["season"], m["episode"]) for m in detail["media"]] == [(1, 1), (1, 2), (2, 1)]
    # move the nameless episode into the series as season 2 episode 2
    nameless = client.get(f"/api/titles/{titles['קבוצת סדרות']['id']}").json()["media"][0]
    client.patch(f"/api/media/{nameless['id']}", json={"move_to_name": "פאודה", "season": 2, "episode": 2})
    detail = client.get(f"/api/titles/{titles['פאודה']['id']}").json()
    assert len(detail["media"]) == 4
    assert client.get(f"/api/titles/{titles['קבוצת סדרות']['id']}").status_code == 404
    # hide
    client.patch(f"/api/media/{nameless['id']}", json={"hidden": True})
    assert len(client.get(f"/api/titles/{titles['פאודה']['id']}").json()["media"]) == 3
    assert len(client.get(f"/api/titles/{titles['פאודה']['id']}?hidden=true").json()["media"]) == 4


def test_stream_ranges(client):
    login(client)
    scan(client)
    mid = client.get("/api/library").json()["titles"][-1]["first_media_id"]
    r = client.get(f"/api/stream/{mid}", headers={"Range": "bytes=1000-2500000"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 1000-2500000/{len(FILE)}"
    assert r.content == FILE[1000:2500001]
    r = client.get(f"/api/stream/{mid}", headers={"Range": "bytes=-100"})
    assert r.content == FILE[-100:]
    r = client.get(f"/api/stream/{mid}", headers={"Range": f"bytes={len(FILE)}-"})
    assert r.status_code == 416
    r = client.get(f"/api/stream/{mid}")
    assert r.status_code == 200 and len(r.content) == len(FILE)


def test_dialogs_and_status(client):
    login(client)
    assert client.get("/api/dialogs").json()[0]["enabled"] is False
    client.post("/api/chats", json={"chats": [{"id": "-100", "title": "x", "enabled": True}]})
    assert client.get("/api/dialogs").json()[0]["enabled"] is True
    st = client.get("/api/status").json()
    assert st["telegram_authorized"] and st["chats"][0]["id"] == -100


def test_season_per_channel(client):
    login(client)
    tg = client.app.state.tg
    tg.messages = [(1, vm(1, "וואן פיס פרק 1")), (2, vm(2, "וואן פיס פרק 2"))]
    scan(client)
    # second channel = second saga, captions say nothing about a season
    second = [(10, VideoMessage(-200, 10, "וואן פיס פרק 62", "", len(FILE), "video/mp4", 1500, 0, 0, 2000)),
              (11, VideoMessage(-200, 11, "וואן פיס עונה 5 פרק 63", "", len(FILE), "video/mp4", 1500, 0, 0, 2001))]

    async def iter_messages(chat_id, min_id=0):
        for mid, v in (second if chat_id == -200 else tg.messages):
            if mid > min_id:
                yield mid, v
    tg.iter_messages = iter_messages
    client.post("/api/chats", json={"chats": [{"id": -200, "title": "סאגת אלבסטה", "enabled": True}]})
    import asyncio
    asyncio.run(client.app.state.scanner.scan())
    tid = client.get("/api/library").json()["titles"][0]["id"]
    seasons = lambda: [(m["season"], m["episode"]) for m in client.get(f"/api/titles/{tid}").json()["media"]]
    assert seasons() == [(1, 1), (1, 2), (1, 62), (5, 63)]
    # assign season 2 to the second channel -> existing episodes move, explicit "עונה 5" stays
    client.post("/api/chats", json={"chats": [
        {"id": -100, "title": "איסט בלו", "enabled": True, "season": "1", "season_label": "איסט בלו"},
        {"id": -200, "title": "סאגת אלבסטה", "enabled": True, "season": "2", "season_label": "אלבסטה"}]})
    assert seasons() == [(1, 1), (1, 2), (2, 62), (5, 63)]
    labels = client.get(f"/api/titles/{tid}").json()["season_labels"]
    assert labels == {"1": "איסט בלו", "2": "אלבסטה"}
    d = {x["id"]: x for x in client.get("/api/dialogs").json()}
    # new episodes from that channel also get season 2
    second.append((12, VideoMessage(-200, 12, "וואן פיס פרק 64", "", len(FILE), "video/mp4", 1500, 0, 0, 2002)))
    asyncio.run(client.app.state.scanner.scan())
    assert (2, 64) in seasons()
    # clearing the season puts them back to automatic
    client.post("/api/chats", json={"chats": [{"id": -200, "title": "סאגת אלבסטה", "enabled": True, "season": ""}]})
    assert (1, 62) in seasons() and (1, 64) in seasons()
