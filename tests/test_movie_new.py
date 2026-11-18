"""Movies: the next movie of a movie series you watched gets "סרט המשך" and shows in "חדש בשבילך"."""
from test_api import client  # noqa: F401 - pytest fixture
import time

from app.db import Database, mark_sequels


def _movie(db, name, year, coll, msg):
    tid = db.get_or_create_title("movie", name, name, year)
    db.update_title(tid, collection_id=coll)
    mid = db.add_media(chat_id=-1, msg_id=msg, title_id=tid, season=None, episode=None, file_name=f"{name}.mp4",
                       caption=name, size=1, mime="video/mp4", duration=5400, width=1, height=1, added_at=time.time())
    return tid, mid


def test_sequel_of_a_watched_movie(tmp_path):
    db = Database(tmp_path / "x.db")
    a, ma = _movie(db, "אבא גנוב", 1987, 77, 1)
    b, _ = _movie(db, "אבא גנוב 2", 1989, 77, 2)
    c, _ = _movie(db, "סרט אחר", 2020, 0, 3)
    rows = {t["id"]: t for t in db.library_titles()}
    assert not any(t["sequel"] for t in rows.values())  # nothing watched yet
    db.save_progress(ma, 5000, 5400)
    rows = {t["id"]: t for t in db.library_titles()}
    assert rows[b]["sequel"] == 1 and rows[a]["sequel"] == 0 and rows[c]["sequel"] == 0


def test_an_earlier_movie_is_not_a_sequel():
    rows = [{"id": 1, "kind": "movie", "collection_id": 5, "year": 2010, "last_watched": 1, "watched_count": 1},
            {"id": 2, "kind": "movie", "collection_id": 5, "year": 2005, "last_watched": None, "watched_count": 0}]
    mark_sequels(rows)
    assert rows[1]["sequel"] == 0


def test_new_on_the_site_by_the_day_it_arrived(tmp_path):
    from app.library import add_video
    from app.telegram import VideoMessage

    db = Database(tmp_path / "y.db")
    old = time.time() - 400 * 86400  # posted on Telegram long ago
    chat = {"id": -5, "title": "סרטים", "last_msg_id": 0, "default_season": None}
    # a group's very first scan: its old posts are not "new"
    add_video(db, VideoMessage(-5, 1, "הקיץ הישן 2010", "", 10, "video/mp4", 5400, 0, 0, old), chat)
    chat["last_msg_id"] = 1
    # later scans: whatever they find is new on the site, even if it came out in cinemas long ago
    add_video(db, VideoMessage(-5, 2, "הלילה החדש 2023", "", 10, "video/mp4", 5400, 0, 0, old), chat)
    add_video(db, VideoMessage(-5, 3, "המשפחה שלנו עונה 1 פרק 1", "", 10, "video/mp4", 1500, 0, 0, old), chat)
    rows = {t["name"]: t for t in db.library_titles()}
    now = time.time()
    assert rows["הקיץ הישן"]["first_found"] < now - 300 * 86400
    assert rows["הלילה החדש"]["first_found"] > now - 60
    s = rows["המשפחה שלנו"]
    assert s["ep_new_at"] > now - 60 and (s["ep_new_season"], s["ep_new_episode"]) == (1, 1)
    # another upload of an episode that was already here is not a new episode
    db.x("UPDATE media SET found_at = ? WHERE chat_id = -5 AND msg_id = 3", (old,))
    add_video(db, VideoMessage(-5, 4, "המשפחה שלנו עונה 1 פרק 1 1080p", "", 10, "video/mp4", 1500, 0, 0, old), chat)
    assert db.site_new_episodes(now - 86400) == {}


def test_badges_in_the_api(client):
    from test_api import login, scan

    login(client)
    scan(client)
    db = client.app.state.db
    lib = client.get("/api/library").json()["titles"]
    assert {t["site_new"] for t in lib} <= {None, "movie", "episode"}
    db.x("UPDATE media SET found_at = ?", (time.time() - 10 * 86400,))
    lib = {t["kind"]: t for t in client.get("/api/library").json()["titles"]}
    assert lib["movie"]["site_new"] == "movie"  # 10 days < 30
    assert lib["series"]["site_new"] is None  # 10 days > 7
    client.post("/api/settings", json={"new_days": {"movie": 5, "series": 14, "finale": 30}})
    lib = {t["kind"]: t for t in client.get("/api/library").json()["titles"]}
    assert lib["movie"]["site_new"] is None and lib["series"]["site_new"] == "episode"
