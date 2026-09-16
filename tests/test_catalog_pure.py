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
