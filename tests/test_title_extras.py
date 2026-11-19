"""Title page extras: trailer, "עוד בסגנון הזה", next episode, actor page."""
import json
import time

from test_api import client, login, scan  # noqa: F401 - pytest fixture

from app.tmdb import parse_extras, pick_trailer


def test_trailer_hebrew_first_then_official_trailer():
    vids = [{"site": "YouTube", "key": "en1", "type": "Teaser", "iso_639_1": "en", "official": True},
            {"site": "YouTube", "key": "en2", "type": "Trailer", "iso_639_1": "en", "official": True},
            {"site": "Vimeo", "key": "x", "type": "Trailer", "iso_639_1": "he"},
            {"site": "YouTube", "key": "bts", "type": "Behind the Scenes", "iso_639_1": "he"}]
    assert pick_trailer(vids)["key"] == "en2"
    vids.append({"site": "YouTube", "key": "he1", "type": "Teaser", "iso_639_1": "he"})
    assert pick_trailer(vids) == {"key": "he1", "hebrew": True, "name": None}
    assert pick_trailer([]) is None


def test_parse_extras():
    ex = parse_extras({"recommendations": {"results": [{"id": 1}, {"id": 2}]},
                       "similar": {"results": [{"id": 2}, {"id": 3}]},
                       "next_episode_to_air": {"air_date": "2030-01-02", "season_number": 3, "episode_number": 6},
                       "status": "Returning Series"})
    assert ex["related"] == [1, 2, 3] and ex["next"]["episode"] == 6 and ex["trailer"] is None


def test_extras_from_saved_tmdb_data_and_person_page(client):
    login(client)
    scan(client)
    db = client.app.state.db
    lib = client.get("/api/library").json()["titles"]
    s = next(t for t in lib if t["kind"] == "series")
    other = db.get_or_create_title("series", "שכנים", "שכנים", 2020)
    db.add_media(chat_id=-100, msg_id=999, title_id=other, season=1, episode=1, file_name="a.mp4", caption="",
                 size=1, mime="video/mp4", duration=1500, width=1, height=1, added_at=time.time())
    db.update_title(other, tmdb_id=555, cast_json=json.dumps([{"name": "ליאור רז", "photo": None}], ensure_ascii=False))
    db.update_title(s["id"], tmdb_id=444, cast_json=json.dumps([{"name": "ליאור רז", "photo": "p.jpg"}], ensure_ascii=False))
    ex = {"tmdb_id": 444, "trailer": {"key": "abc", "hebrew": False, "name": "T"}, "related": [555, 999],
          "next": {"date": "2099-05-01", "season": 5, "episode": 1, "name": None}, "status": "Returning Series"}
    db.x("INSERT INTO tmdb_extras VALUES (?, ?, ?)", (s["id"], json.dumps(ex), time.time()))
    r = client.get(f"/api/titles/{s['id']}/extras").json()
    assert r["trailer"]["key"] == "abc" and r["next"]["episode"] == 1 and r["ended"] is False
    assert [t["id"] for t in r["more"]][:1] == [other]  # only what is in your library
    # an episode that already aired is not "next"
    ex["next"]["date"] = "2001-01-01"
    db.x("UPDATE tmdb_extras SET data = ? WHERE title_id = ?", (json.dumps(ex), s["id"]))
    assert client.get(f"/api/titles/{s['id']}/extras").json()["next"] is None

    p = client.get("/api/person", params={"name": "ליאור רז"}).json()
    assert {t["id"] for t in p["titles"]} == {s["id"], other} and p["photo"] == "p.jpg"
    assert client.get("/api/person", params={"name": "ליאור"}).json()["titles"] == []  # the whole name only
