import pytest

from app.parser import normalize, parse_media


@pytest.mark.parametrize(
    "caption,file_name,expected",
    [
        (None, "Breaking.Bad.S02E05.720p.WEB-DL.mkv", ("Breaking Bad", "series", 2, 5)),
        ("", "the_office_3x07_hebsub.mp4", ("the office", "series", 3, 7)),
        ("פאודה עונה 2 פרק 5", None, ("פאודה", "series", 2, 5)),
        ("שטיסל - עונה 3, פרק 12\nבאיכות גבוהה", None, ("שטיסל", "series", 3, 12)),
        ("#הבורר ע2 פ8", None, ("הבורר", "series", 2, 8)),
        ("עבודה ערבית פרק 4", None, ("עבודה ערבית", "series", 1, 4)),
        ("Friends Season 4 Episode 10", None, ("Friends", "series", 4, 10)),
        ("פרק 6", "Shtisel.S01E06.mp4", ("Shtisel", "series", 1, 6)),
    ],
)
def test_series(caption, file_name, expected):
    info = parse_media(caption, file_name)
    assert (info.name.lower(), info.kind, info.season, info.episode) == (
        expected[0].lower(),
        expected[1],
        expected[2],
        expected[3],
    )


def test_episode_without_name_uses_fallback():
    info = parse_media("פרק 3", None, fallback_name="קופה ראשית")
    assert info.name == "קופה ראשית"
    assert (info.kind, info.season, info.episode) == ("series", 1, 3)


def test_movie_with_year():
    info = parse_media(None, "Inception.2010.1080p.BluRay.x264.mkv")
    assert info.kind == "movie"
    assert info.name == "Inception"
    assert info.year == 2010


def test_movie_hebrew_caption():
    info = parse_media("הסרט: גבעת חלפון אינה עונה (1976) מתורגם", None)
    assert info.kind == "movie"
    assert info.year == 1976
    assert "גבעת חלפון" in info.name


def test_normalize_groups_variants():
    assert normalize("The Office") == normalize("office")
    assert normalize("Grey's Anatomy") == normalize("Greys  Anatomy")
