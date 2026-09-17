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
