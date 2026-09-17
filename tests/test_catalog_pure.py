import json

from catalog import refresh


def test_refresh_indexes_metadata_files_and_subtitle_languages(tmp_path):
    video = tmp_path / "channel" / "video-id"
    video.mkdir(parents=True)
    (video / "channel.video-id.20260101.Title [video-id].out.info.json").write_text(
        json.dumps({"title": "Title", "uploader": "Artist", "tags": ["music"]}),
        encoding="utf-8",
    )
    (video / "Title [video-id].out.en.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (video / "Title [video-id].out.en-orig.vtt").write_text("WEBVTT\n", encoding="utf-8")

    db = tmp_path / "catalog.db"
    assert refresh(tmp_path, db) == 1

    import sqlite3
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT artist, album, files_json FROM videos").fetchone()
        tags = conn.execute("SELECT tag FROM video_tags").fetchall()
    assert row[0] == "Artist"
    assert row[1] == "Uncategorized"
    files = json.loads(row[2])
    assert files["Title [video-id].out.en.vtt"]["subtitle_language"] == "en"
    assert files["Title [video-id].out.en.vtt"]["source_root"] == tmp_path.name
    assert files["Title [video-id].out.en-orig.vtt"]["subtitle_language"] == "en-orig"
    assert tags == [("music",)]


def test_refresh_restores_playlist_from_descriptor(tmp_path):
    playlist_dir = tmp_path / "playlists"
    playlist_dir.mkdir()
    (playlist_dir / "PLexample.info.json").write_text(json.dumps({
        "_type": "playlist",
        "id": "PLexample",
        "title": "Saved playlist",
        "webpage_url": "https://www.youtube.com/playlist?list=PLexample",
        "entries": [{
            "id": "dQw4w9WgXcQ", "title": "First", "playlist_index": 1,
            "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        }],
    }), encoding="utf-8")

    db = tmp_path / "catalog.db"
    assert refresh(tmp_path / "media", db, playlist_metadata_dir=playlist_dir) == 0

    import sqlite3
    with sqlite3.connect(db) as conn:
        playlist = conn.execute("SELECT playlist_id, title FROM playlists").fetchone()
        member = conn.execute("SELECT video_id, playlist_index FROM playlist_videos").fetchone()
    assert playlist == ("PLexample", "Saved playlist")
    assert member == ("dQw4w9WgXcQ", 1)


def test_refresh_keeps_renamed_media_roots_on_same_catalog_row(tmp_path):
    import sqlite3

    for old_name, new_name, expected_root in (
        ("merged", "media-strict", "merged"),
        ("legacy", "media-legacy", "legacy"),
        ("bestfallback", "media-fallback", "bestfallback"),
    ):
        old_video = tmp_path / old_name / "channel" / "video-id"
        new_video = tmp_path / new_name / "channel" / "video-id"
        old_video.mkdir(parents=True)
        new_video.mkdir(parents=True)
        info = json.dumps({"title": "Title", "uploader": "Artist"})
        for video_dir in (old_video, new_video):
            (video_dir / "video-id.out.info.json").write_text(info, encoding="utf-8")

        db = tmp_path / f"{new_name}.db"
        assert refresh(old_video.parents[1], db) == 1
        assert refresh(new_video.parents[1], db) == 1

        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT video_id, source_root FROM videos"
            ).fetchall()
        assert rows == [("video-id", expected_root)]
