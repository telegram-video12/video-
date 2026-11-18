"""Movies: the next movie of a movie series you watched gets "סרט המשך" and shows in "חדש בשבילך"."""
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
