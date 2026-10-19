from library_helpers import clean_display_title, human_date


def test_clean_display_title_removes_artist_and_official_suffix():
    assert clean_display_title("Artist - Song (Official Music Video)", "Artist") == "Song"
    assert clean_display_title("Artist — Song [Official Video]", "Artist") == "Song"
    assert clean_display_title("Song", "Artist") == "Song"


def test_human_date_formats_valid_yt_dlp_date():
    assert human_date("20261016") == "Oct 16, 2026"
    assert human_date("unknown") == "unknown"
