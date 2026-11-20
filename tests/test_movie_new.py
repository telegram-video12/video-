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
    db.always_fresh = True
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


def test_release_dates_from_tmdb():
    from app.tmdb import _day, available

    movie = {"release_date": "2026-03-01", "release_dates": {"results": [
        {"iso_3166_1": "US", "release_dates": [{"type": 3, "release_date": "2026-03-01T00:00:00.000Z"},
                                               {"type": 4, "release_date": "2026-11-10T00:00:00.000Z"}]},
        {"iso_3166_1": "IL", "release_dates": [{"type": 4, "release_date": "2026-11-12T00:00:00.000Z"}]}]}}
    assert available("movie", movie)["avail_at"] == _day("2026-11-10")  # digital, not the cinema
    assert available("movie", {"release_date": "2025-01-05"})["avail_at"] == _day("2025-01-05")
    assert available("movie", {})["avail_at"] == 0
    tv = {"last_episode_to_air": {"air_date": "2026-11-15", "season_number": 2, "episode_number": 8,
                                  "episode_type": "finale"}}
    assert available("tv", tv) == {"avail_at": _day("2026-11-15"), "last_ep_season": 2, "last_ep_number": 8,
                                   "last_ep_finale": 1}


def test_badges_in_the_api(client):
    from test_api import login, scan

    login(client)
    scan(client)
    db = client.app.state.db
    lib = {t["kind"]: t for t in client.get("/api/library").json()["titles"]}
    assert lib["movie"]["site_new"] is None  # TMDB said nothing yet
    now = time.time()
    db.x("UPDATE titles SET avail_at = ? WHERE kind = 'movie'", (now - 10 * 86400,))
    sid = lib["series"]["id"]
    eps = client.get(f"/api/titles/{sid}").json()["media"]
    last = max(eps, key=lambda m: ((m["season"] or 1), m["episode"] or 0))
    # TMDB: the latest episode aired 3 days ago, but it is not on the site yet -> nothing new
    db.x("UPDATE titles SET avail_at = ?, last_ep_season = ?, last_ep_number = ?, last_ep_finale = 0 WHERE id = ?",
         (now - 3 * 86400, last["season"] or 1, (last["episode"] or 0) + 1, sid))
    lib = {t["kind"]: t for t in client.get("/api/library").json()["titles"]}
    assert lib["movie"]["site_new"] == "movie" and lib["series"]["site_new"] is None
    # ... it is on the site
    db.x("UPDATE titles SET last_ep_number = ? WHERE id = ?", (last["episode"], sid))
    lib = {t["kind"]: t for t in client.get("/api/library").json()["titles"]}
    assert lib["series"]["site_new"] == "episode"
    # 10 days ago: past the 7 days, unless it ended the season (30)
    db.x("UPDATE titles SET avail_at = ? WHERE id = ?", (now - 10 * 86400, sid))
    assert {t["kind"]: t for t in client.get("/api/library").json()["titles"]}["series"]["site_new"] is None
    db.x("UPDATE titles SET last_ep_finale = 1 WHERE id = ?", (sid,))
    assert {t["kind"]: t for t in client.get("/api/library").json()["titles"]}["series"]["site_new"] == "episode"
    client.post("/api/settings", json={"new_days": {"movie": 5, "series": 7, "finale": 30}})
    lib = {t["kind"]: t for t in client.get("/api/library").json()["titles"]}
    assert lib["movie"]["site_new"] is None


def test_new_rows_order_and_unreleased_sequels(client):
    from test_api import login, scan

    login(client)
    scan(client)
    db = client.app.state.db
    now = time.time()
    lib = client.get("/api/library").json()["titles"]
    series = [t for t in lib if t["kind"] == "series"]
    movie = next(t for t in lib if t["kind"] == "movie")
    s = series[-1]  # not the first one: it must move to the front
    eps = client.get(f"/api/titles/{s['id']}").json()["media"]
    last = max(eps, key=lambda m: ((m["season"] or 1), m["episode"] or 0))
    db.x("UPDATE titles SET year = 2020")
    db.x("UPDATE titles SET avail_at = ?, last_ep_season = ?, last_ep_number = ? WHERE id = ?",
         (now - 86400, last["season"] or 1, last["episode"], s["id"]))
    db.x("UPDATE titles SET avail_at = ? WHERE id = ?", (now - 2 * 86400, movie["id"]))
    row = client.get("/api/library?kind=series&sort=release&newfirst=1").json()["titles"]
    assert row[0]["id"] == s["id"] and row[0]["site_new"] == "episode"
    # "חדש בשבילך": also series you never started and new movies
    new = [t["id"] for t in client.get("/api/home").json()["new"]]
    assert new.index(s["id"]) < new.index(movie["id"])


def test_unreleased_movie_is_not_a_sequel():
    rows = [{"id": 1, "kind": "movie", "collection_id": 5, "year": 2010, "last_watched": 1, "watched_count": 1},
            {"id": 2, "kind": "movie", "collection_id": 5, "year": 2099, "last_watched": None, "watched_count": 0},
            {"id": 3, "kind": "movie", "collection_id": 5, "year": 2015, "last_watched": None, "watched_count": 0,
             "avail_at": time.time() + 86400 * 30}]
    mark_sequels(rows)
    assert rows[1]["sequel"] == 0 and rows[2]["sequel"] == 0


def test_site_counts_ignore_filters(client):
    from test_api import login, scan

    login(client)
    scan(client)
    full = client.get("/api/library?kind=movie").json()["site_counts"]
    filtered = client.get("/api/library?kind=movie&genres=nothing").json()
    assert filtered["total"] == 0 and filtered["site_counts"] == full and full["movie"] >= 1


def test_israeli_genre_by_tmdb(tmp_path):
    from app.tmdb import made_in_israel

    assert made_in_israel({"original_language": "en", "origin_country": ["US"]}) == 0
    assert made_in_israel({"original_language": "he"}) == 1
    assert made_in_israel({"original_language": "en", "production_countries": [{"iso_3166_1": "IL"}]}) == 1

    db = Database(tmp_path / "z.db")
    a, _ = _movie(db, "חוות דאטון", 2025, 0, 1)  # shown on yes: the caption made it "il"
    b, _ = _movie(db, "חצויה", 2016, 0, 2)
    c, _ = _movie(db, "לא נבדק", 2016, 0, 3)
    db.update_title(a, origin="il", made_in_il=0)
    db.update_title(b, made_in_il=1)
    db.update_title(c, origin="il")  # TMDB does not know it: the group/caption decides
    g = {t["id"]: "ישראלי" in t["genres"] for t in db.library_titles()}
    assert g == {a: False, b: True, c: True}


def test_library_list_refreshes_in_the_background(tmp_path):
    db = Database(tmp_path / "w.db")
    a, _ = _movie(db, "ראשון", 2001, 0, 1)
    assert db.library_titles()[0]["name"] == "ראשון"
    db.update_title(a, soft=True, poster="p.jpg")  # a background update (TMDB): served from the old list at once
    db.soft_refresh_seconds = 0
    assert db.library_titles()[0]["poster"] is None
    for _ in range(100):  # ... and refreshed in the background
        if db.library_titles()[0]["poster"] == "p.jpg":
            break
        time.sleep(0.02)
    assert db.library_titles()[0]["poster"] == "p.jpg"
    db.wants_fresh = True
    db.update_title(a, name="שונה")  # your own change: right away
    assert db.library_titles()[0]["name"] == "שונה"


def test_list_saved_for_the_next_start_and_own_changes_wait(tmp_path):
    db = Database(tmp_path / "s.db")
    a, _ = _movie(db, "שמור", 2001, 0, 1)
    db.library_titles()
    db2 = Database(tmp_path / "s.db")  # the site starts again: the saved list is there at once
    assert db2._titles_cache[0] == -1 and db2.library_titles()[0]["name"] == "שמור"
    db.x("UPDATE titles SET name = 'אחר'")  # a background change: the previous list at once
    assert db.library_titles()[0]["name"] == "שמור"
    db.wants_fresh = True
    db.x("UPDATE titles SET name = 'שלי'")  # your own change: waited for
    assert db.library_titles()[0]["name"] == "שלי"


def test_anime_genre(tmp_path):
    import json as _json
    db = Database(tmp_path / "an.db")
    db.always_fresh = True
    a, _ = _movie(db, "וואן פיס", 1999, 0, 1)
    b, _ = _movie(db, "שרק", 2001, 0, 2)
    c, _ = _movie(db, "סרט סיני", 2019, 0, 3)
    d, _ = _movie(db, "סדרה יפנית", 2020, 0, 4)
    anim = _json.dumps(["אנימציה", "הרפתקאות"], ensure_ascii=False)
    db.update_title(a, genres=anim, original_name="ワンピース")
    db.update_title(b, genres=anim, original_name="Shrek")
    db.update_title(c, genres=anim, original_name="哪吒之魔童降世")  # Chinese: not anime
    db.update_title(d, genres=anim, original_name="Shingeki", orig_lang="ja")
    g = {t["id"]: "אנימה" in t["genres"] for t in db.library_titles()}
    assert g == {a: True, b: False, c: False, d: True}
