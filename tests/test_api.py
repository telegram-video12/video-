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


def test_peeking_does_not_move_resume_point(client):
    login(client)
    scan(client)
    fauda = next(t for t in client.get("/api/library").json()["titles"] if t["name"] == "פאודה")
    ep1, ep2 = client.get(f"/api/titles/{fauda['id']}").json()["media"]
    client.post(f"/api/progress/{ep1['id']}", json={"position": 600, "duration": 1500})
    client.post(f"/api/progress/{ep2['id']}", json={"position": 3, "duration": 1500})  # opened by mistake
    resume = client.get(f"/api/titles/{fauda['id']}").json()["resume"]
    assert resume["media"]["id"] == ep1["id"] and resume["position"] == 600


def test_duplicates_show_once_best_quality_first(client):
    login(client)
    tg = client.app.state.tg

    def mv(mid, caption, name="", height=None, size=len(FILE), chat=-100):
        return VideoMessage(chat, mid, caption, name, size, "video/mp4", 6000, None, height, 1000 + mid)
    tg.messages = [
        (1, mv(1, "הסרט: אינספשן (2010) 720p")),
        (2, mv(2, "אינספשן 2010 1080p", height=1080)),
        (3, mv(3, "", "Inception.2010.480p.mkv")),       # English name: separate until TMDB/merge
        (4, mv(4, "אינספשן (2010) מתורגם", height=720)),
        (5, mv(5, "שטיסל עונה 1 פרק 1 720p")),
        (6, mv(6, "שטיסל עונה 1 פרק 1 1080p")),
        (7, mv(7, "שטיסל עונה 1 פרק 2")),
    ]
    scan(client)
    titles = {t["name"]: t for t in client.get("/api/library").json()["titles"]}
    assert titles["אינספשן"]["media_count"] == 1          # 3 uploads -> one movie
    assert titles["שטיסל"]["media_count"] == 2            # 3 uploads -> two episodes
    movie = client.get(f"/api/titles/{titles['אינספשן']['id']}").json()
    assert len(movie["media"]) == 1
    best = movie["media"][0]
    assert best["quality"] == 1080 and best["versions"] == 3
    # watching a lower version counts for the movie, and the best version resumes from there
    detail = client.get(f"/api/media/{best['id']}").json()
    assert [v["quality"] for v in detail["versions"]] == [1080, 720, 720]
    low = detail["versions"][-1]["id"]
    client.post(f"/api/progress/{low}", json={"position": 900, "duration": 6000})
    assert client.get(f"/api/media/{best['id']}").json()["media"]["position"] == 900
    assert client.get(f"/api/titles/{titles['אינספשן']['id']}").json()["media"][0]["position"] == 900
    # edit mode still lists every file
    assert len(client.get(f"/api/titles/{titles['אינספשן']['id']}?hidden=true").json()["media"]) == 3
    # series: next episode skips the duplicate version
    eps = client.get(f"/api/titles/{titles['שטיסל']['id']}").json()["media"]
    assert eps[0]["quality"] == 1080
    nav = client.get(f"/api/media/{eps[0]['id']}").json()
    assert nav["next"]["episode"] == 2
    # merging the English-named entry makes it a 4th version of the same movie
    client.post(f"/api/titles/{titles['Inception']['id']}/merge", json={"into": titles["אינספשן"]["id"]})
    assert client.get(f"/api/titles/{titles['אינספשן']['id']}").json()["media"][0]["versions"] == 4


def test_tmdb_merges_same_movie_with_different_names(client, monkeypatch):
    import asyncio
    from app.tmdb import TMDB
    login(client)
    tg = client.app.state.tg
    tg.messages = [(1, vm(1, "אינספשן (2010)")), (2, vm(2, "", "Inception.2010.1080p.mkv"))]
    scan(client)
    db = client.app.state.db
    assert len(client.get("/api/library").json()["titles"]) == 2

    async def lookup(self, kind, name, year=None, tmdb_id=None):
        return {"tmdb_id": 27205, "tmdb_name": "התחלה", "overview": "...", "poster": None, "backdrop": None, "year": 2010}
    monkeypatch.setattr(TMDB, "lookup", lookup)
    asyncio.run(TMDB("k").enrich_pending(db))
    titles = client.get("/api/library").json()["titles"]
    assert len(titles) == 1 and titles[0]["name"] == "התחלה" and titles[0]["media_count"] == 1


def test_library_paging_and_search(client):
    login(client)
    tg = client.app.state.tg
    tg.messages = [(i, vm(i, f"סרט מספר {i} (2001)")) for i in range(1, 151)] + \
                  [(500, vm(500, "פאודה עונה 1 פרק 1"))]
    scan(client)
    first = client.get("/api/library?limit=60").json()
    assert first["total"] == 151 and len(first["titles"]) == 60 and "continue" in first
    assert first["counts"] == {"all": 151, "series": 1, "movie": 150}
    rest = client.get("/api/library?offset=120&limit=60").json()
    assert len(rest["titles"]) == 31 and "continue" not in rest
    ids = {t["id"] for t in first["titles"]} | {t["id"] for t in client.get("/api/library?offset=60&limit=60").json()["titles"]} \
        | {t["id"] for t in rest["titles"]}
    assert len(ids) == 151
    assert client.get("/api/library?kind=series").json()["total"] == 1
    found = client.get("/api/library?q=" + "מספר 14").json()
    assert {t["name"] for t in found["titles"]} >= {"סרט מספר 14"} or {t["name"] for t in found["titles"]} >= {"מספר 14"}
    assert found["total"] == 11  # 14, 140..149


MOVIES = ["ספיידרמן 3", "ספיידרמן: השיבה הביתה", "ספיידרמן אין דרך הביתה", "ספיידרמן: רחוק מהבית",
          "הארי פוטר ואבן החכמים", "הארי פוטר וחדר הסודות", "מלך האריות", "שר הטבעות", "טיטאניק", "אווטאר",
          "התחלה", "מטריקס", "גלדיאטור", "פורסט גאמפ", "האביר האפל", "אינטרסטלר", "ג'וקר", "הנוקמים",
          "מועדון קרב", "רשימת שינדלר", "הסנדק", "צעצוע של כאסח", "שרק", "מוצאים את נמו", "למעלה"]


def test_channel_signatures_are_removed(client):
    import asyncio
    login(client)
    tg = client.app.state.tg
    chats = {
        -1: lambda n, i: f"קינג סרט {n} ({2000 + i % 20})",
        -2: lambda n, i: f"מדיה VOD {n}\nהועלה ע\"י יחיאל",
        -3: lambda n, i: f"🎬 חנואל סרטים 🎬\n{n} 1080p",
        -4: lambda n, i: f"{n} 720p",
    }
    msgs = {cid: [(i + 1, VideoMessage(cid, i + 1, fmt(n, i), "", len(FILE), "video/mp4", 5000 + i * 97, None, None, 1000 + i))
                  for i, n in enumerate(MOVIES)] for cid, fmt in chats.items()}

    async def iter_messages(chat_id, min_id=0):
        for mid, v in msgs[chat_id]:
            if mid > min_id:
                yield mid, v
    tg.iter_messages = iter_messages
    client.post("/api/chats", json={"chats": [{"id": c, "title": f"ערוץ {c}", "enabled": True} for c in chats]})
    asyncio.run(client.app.state.scanner.scan())
    lib = client.get("/api/library?limit=200").json()
    names = sorted(t["name"] for t in lib["titles"])
    assert lib["total"] == len(MOVIES), names
    spider = client.get("/api/library?q=ספיידרמן").json()
    assert spider["total"] == 4
    for t in spider["titles"]:
        detail = client.get(f"/api/titles/{t['id']}").json()
        assert len(detail["media"]) == 1 and detail["media"][0]["versions"] == 4
        assert detail["media"][0]["quality"] == 1080          # the 1080p upload is the one that plays
    assert "הארי פוטר ואבן החכמים" in names                  # a real shared word is not a signature
    # a later upload in the same channel is cleaned right away
    msgs[-1].append((100, VideoMessage(-1, 100, "קינג סרט טיטאניק 4K", "", len(FILE), "video/mp4", 5000 + 8 * 97, None, None, 5000)))
    asyncio.run(client.app.state.scanner.scan())
    titanic = client.get("/api/library?q=טיטאניק").json()["titles"]
    assert len(titanic) == 1
    assert client.get(f"/api/titles/{titanic[0]['id']}").json()["media"][0]["quality"] == 2160


def _many_movies(n):
    """n distinct, realistic-looking movie names."""
    a = ["האביר", "המסע", "הבית", "סוד", "לילה", "אהבה", "מלחמת", "הים", "השומר", "המלך", "חלום", "הדרך"]
    b = ["האחרון", "הגדול", "בפריז", "של ג'ון", "השחור", "הנסתר", "בלב", "הלבן", "מהעבר", "בצפון", "הזהב", "הקטן"]
    c = ["", " 2", " 3", " החדש", " הסוף", " הנקמה", " השני", " ההתחלה"]
    out = []
    for i in range(n):
        out.append(f"{a[i % 12]} {b[(i // 12) % 12]}{c[(i // 144) % 8]}")
    return list(dict.fromkeys(out))


def test_big_archive_channel_mixing_many_signatures(client):
    """One huge 'archive' channel that re-posts from many sources, each with its own signature."""
    import asyncio
    login(client)
    tg = client.app.state.tg
    names = _many_movies(1100)
    sigs = [lambda n: f"קינג סרט {n}", lambda n: f"מדיה VOD {n} הועלה ע\"י יחיאל", lambda n: f"פופקורן טיים {n}",
            lambda n: f"ק.ס {n}", lambda n: f"{n} דב ס", lambda n: f"{n}"]
    msgs, mid = [], 0
    for i, n in enumerate(names):
        for k in range(1 + i % 3):  # 1-3 uploads of each movie from different sources
            mid += 1
            msgs.append((mid, VideoMessage(-100, mid, sigs[(i + k) % len(sigs)](n), "", len(FILE), "video/mp4", 4000 + i * 11, None, None, mid)))
    # a franchise appearing many times must keep its name
    for j, part in enumerate(["", " 2", " 3", ": השיבה הביתה", ": רחוק מהבית", ": אין דרך הביתה"] * 3):
        mid += 1
        msgs.append((mid, VideoMessage(-100, mid, sigs[j % 6](f"ספיידרמן{part}"), "", len(FILE), "video/mp4", 20000 + (j % 6) * 400, None, None, mid)))
    tg.messages = msgs
    scan(client)
    lib = client.get("/api/library?limit=1").json()
    assert lib["total"] == len(names) + 6
    spider = client.get("/api/library?q=ספיידרמן&limit=50").json()
    assert sorted(t["name"] for t in spider["titles"]) == sorted(
        ["ספיידרמן", "ספיידרמן 2", "ספיידרמן 3", "ספיידרמן: השיבה הביתה", "ספיידרמן: רחוק מהבית", "ספיידרמן: אין דרך הביתה"])


def test_manual_strip_phrases(client):
    import time
    login(client)
    tg = client.app.state.tg
    tg.messages = [(1, vm(1, "שקד הפקות טיטאניק (1997)")), (2, vm(2, "טיטאניק (1997) 1080p"))]
    scan(client)
    assert client.get("/api/library").json()["total"] == 2   # too few movies to learn the signature
    client.post("/api/settings", json={"strip_phrases": "שקד הפקות\n"})
    for _ in range(50):
        if client.get("/api/library").json()["total"] == 1:
            break
        time.sleep(0.1)
    lib = client.get("/api/library").json()
    assert lib["total"] == 1 and lib["titles"][0]["name"] == "טיטאניק"
    assert client.get("/api/status").json()["strip_phrases"] == ["שקד הפקות"]


def test_tmdb_stops_on_rejected_key_and_hides_it(client, monkeypatch, caplog):
    import asyncio
    import httpx
    from app.tmdb import TMDB
    login(client)
    tg = client.app.state.tg
    tg.messages = [(i, vm(i, f"סרט בדיקה {i} (2001)")) for i in range(1, 40)]
    scan(client)
    calls = []

    async def lookup(self, kind, name, year=None, tmdb_id=None):
        calls.append(name)
        req = httpx.Request("GET", "https://api.themoviedb.org/3/search/movie?api_key=SECRETKEY")
        raise httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req))
    monkeypatch.setattr(TMDB, "lookup", lookup)
    t = TMDB("SECRETKEY")
    asyncio.run(t.enrich_pending(client.app.state.db))
    assert t.stopped and len(calls) <= 6            # gave up after the first batch
    assert "SECRETKEY" not in caplog.text
    # titles are still pending for the next (valid) key
    assert len(client.app.state.db.titles_needing_tmdb()) >= 30


def test_spelling_variants_merge_and_old_libraries_are_rekeyed(client):
    from app.library import migrate_norms
    login(client)
    db = client.app.state.db
    tg = client.app.state.tg
    tg.messages = [(1, vm(1, "ספיידרמן אין דרך הביתה (2021)")), (2, vm(2, "ספיידרמן אין דרך הבייתה (2021) 1080p")),
                   (3, vm(3, "ספיידר-מן 2 (2004)")), (4, vm(4, "ספיידרמן 2 (2004)"))]
    scan(client)
    assert client.get("/api/library").json()["total"] == 2
    # an old library keyed with spaces (before this change) is re-keyed and merged on start-up
    a = db.x("INSERT INTO titles (kind, name, norm_name, created_at, tmdb_id) VALUES ('movie', 'Old A', 'old movie', 0, 77)")
    b = db.x("INSERT INTO titles (kind, name, norm_name, created_at) VALUES ('movie', 'Old B', 'oldmovie', 0)")
    db.x("INSERT INTO media (chat_id, msg_id, title_id, size, added_at, auto_norm) VALUES (-100, 900, ?, 1, 0, 'old movie')", (a,))
    db.x("INSERT INTO media (chat_id, msg_id, title_id, size, added_at, auto_norm) VALUES (-100, 901, ?, 1, 0, 'oldmovie')", (b,))
    db.set_pref("norm_version", "1")
    assert migrate_norms(db) >= 1
    kept = db.q("SELECT * FROM titles WHERE norm_name = 'oldmovie'")
    assert len(kept) == 1 and kept[0]["tmdb_id"] == 77          # the one with TMDB info survives
    assert {m["title_id"] for m in db.q("SELECT title_id FROM media WHERE msg_id IN (900, 901)")} == {kept[0]["id"]}
    assert db.q1("SELECT COUNT(*) c FROM media WHERE auto_norm = 'old movie'")["c"] == 0


def test_library_filters_and_sorting(client):
    login(client)
    tg = client.app.state.tg
    tg.messages = [(1, vm(1, "בטא (1995)")), (2, vm(2, "אלפא (2012)")), (3, vm(3, "גמא (2021)")),
                   (4, vm(4, "Zulu (2003)")), (5, vm(5, "דלתא עונה 1 פרק 1")), (6, vm(6, "דלתא עונה 1 פרק 2"))]
    scan(client)
    lib = lambda qs: client.get("/api/library?" + qs).json()
    ids = {t["name"]: t["first_media_id"] for t in lib("limit=50")["titles"]}
    client.post(f"/api/progress/{ids['אלפא']}", json={"position": 600, "duration": 1500})   # watching
    client.post(f"/api/watched/{ids['גמא']}", json={"watched": True})                        # done
    client.post(f"/api/progress/{ids['בטא']}", json={"position": 3, "duration": 1500})       # a peek: still new
    names = lambda qs: [t["name"] for t in lib(qs)["titles"]]
    assert names("kind=movie&state=watching") == ["אלפא"]
    assert names("kind=movie&state=done") == ["גמא"]
    assert sorted(names("kind=movie&state=new")) == sorted(["בטא", "Zulu"])
    assert names("kind=movie&sort=name") == ["אלפא", "בטא", "גמא", "Zulu"]       # Hebrew first, then English
    assert names("kind=movie&sort=year") == ["גמא", "אלפא", "Zulu", "בטא"]
    assert names("kind=movie&decade=2000") == ["Zulu"]
    assert names("kind=movie&decade=1990") == ["בטא"] and names("kind=movie&decade=old") == []
    assert names("kind=movie&sort=watched")[:2] == ["גמא", "אלפא"]
    assert lib("kind=series&state=new")["total"] == 1


def test_upload_named_like_tmdb_title_joins_it_and_remakes_stay_apart(client, monkeypatch):
    import asyncio
    from app.tmdb import TMDB
    login(client)
    tg = client.app.state.tg
    tg.messages = [(1, vm(1, "Spider-Man No Way Home (2021)")), (2, vm(2, "מלך האריות (1994)"))]
    scan(client)

    async def lookup(self, kind, name, year=None, tmdb_id=None):
        heb = {"Spider-Man No Way Home": "ספיידרמן: אין דרך הביתה", "מלך האריות": "מלך האריות"}[name]
        return {"tmdb_id": hash(name) % 1000, "tmdb_name": heb, "overview": "", "poster": None, "backdrop": None, "year": year}
    monkeypatch.setattr(TMDB, "lookup", lookup)
    db = client.app.state.db
    asyncio.run(TMDB("k").enrich_pending(db))
    # a later Hebrew upload, spelled differently, joins the TMDB-named entry
    tg.messages = tg.messages + [(3, vm(3, "ספיידרמן אין דרך הבייתה (2021)")), (4, vm(4, "מלך האריות (2019)"))]
    scan(client)
    names = sorted(t["name"] for t in client.get("/api/library?limit=50").json()["titles"])
    assert names.count("ספיידרמן: אין דרך הביתה") == 1 and len([n for n in names if "ספיידרמן" in n]) == 1
    assert names.count("מלך האריות") == 2          # 1994 and the 2019 remake stay separate
    # and an entry that existed before TMDB named the other one is merged too
    from app.parser import normalize
    db.x("INSERT INTO titles (kind, name, norm_name, name_key, year, created_at) VALUES ('movie','x',?,'x',2021,0)",
         (normalize("ספיידרמן אין דרך הבייתה"),))
    assert db.merge_by_name() >= 1
