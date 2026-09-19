import json

from catalog import (
    library_cards,
    library_exports,
    library_files,
    library_tags,
    playlists,
    queue_add,
    queue_items,
    record_attempt,
    record_playlist_membership,
    refresh,
    set_setting,
    setting,
)


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


def test_playlists_report_available_and_missing_members(tmp_path):
    root = tmp_path / "media-strict"
    _make_video(root, "channel", "aaaaaaaaaaa", "Available")
    missing = root / "channel" / "bbbbbbbbbbb"
    missing.mkdir(parents=True)
    (missing / "missing.out.info.json").write_text(json.dumps({"title": "Missing"}), encoding="utf-8")
    db = tmp_path / "catalog.db"
    assert refresh(root, db) == 2
    record_playlist_membership(db, "PLexample", "Example", None, [
        {"video_id": "aaaaaaaaaaa", "playlist_index": 1, "title": "Available"},
        {"video_id": "bbbbbbbbbbb", "playlist_index": 2, "title": "Missing"},
    ])

    summary = playlists(db)[0]
    assert summary["video_count"] == 2
    assert summary["available_count"] == 1
    assert summary["missing_count"] == 1


def _make_video(root, channel, video_id, title="Title", extra_info=None):
    video = root / channel / video_id
    video.mkdir(parents=True, exist_ok=True)
    info = {"title": title, "uploader": "Artist", "upload_date": "20260101",
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}"}
    if extra_info:
        info.update(extra_info)
    (video / f"{channel}.{video_id}.20260101.{title} [{video_id}].out.info.json").write_text(
        json.dumps(info), encoding="utf-8")
    (video / f"{channel}.{video_id}.20260101.{title} [{video_id}].out.mkv").write_bytes(b"v")
    (video / f"{channel}.{video_id}.20260101.{title} [{video_id}].out.m4a").write_bytes(b"a")
    return video


def test_library_cards_paginate_and_rebuild_from_disk(tmp_path):
    root = tmp_path / "media-strict"
    for vid, title in (("aaaaaaaaaaa", "Alpha"), ("bbbbbbbbbbb", "Beta"), ("ccccccccccc", "Gamma")):
        _make_video(root, "channel", vid, title)
    db = tmp_path / "catalog.db"
    assert refresh(root, db) == 3

    items, total = library_cards(db)
    assert total == 3
    assert {item["video_id"] for item in items} == {"aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"}
    card = next(item for item in items if item["video_id"] == "aaaaaaaaaaa")
    assert card["title"] == "Alpha"
    assert card["date"] == "20260101"
    assert card["files"]["video"].endswith(".out.mkv")
    assert card["files"]["audio"].endswith(".out.m4a")

    page1, total1 = library_cards(db, page=1, per_page=2)
    page2, _ = library_cards(db, page=2, per_page=2)
    assert total1 == 3
    assert len(page1) == 2 and len(page2) == 1
    assert {item["video_id"] for item in page1} | {item["video_id"] for item in page2} == \
        {item["video_id"] for item in items}

    # Deleting the DB and rescanning restores identical cards (only scanned_at changes,
    # which cards do not expose).
    snapshot = sorted((item["video_id"], item["title"], item["date"],
                       item["files"]["video"], item["files"]["audio"]) for item in items)
    db.unlink()
    assert refresh(root, db) == 3
    rebuilt, _ = library_cards(db)
    assert sorted((item["video_id"], item["title"], item["date"],
                   item["files"]["video"], item["files"]["audio"]) for item in rebuilt) == snapshot


def test_library_cards_search_filters_title_video_and_channel(tmp_path):
    root = tmp_path / "media-strict"
    _make_video(root, "chan-alpha", "aaaaaaaaaaa", "Alpha Song")
    _make_video(root, "chan-beta", "bbbbbbbbbbb", "Beta Tune")
    db = tmp_path / "catalog.db"
    assert refresh(root, db) == 2

    items, total = library_cards(db, q="alpha")
    assert total == 1
    assert {item["video_id"] for item in items} == {"aaaaaaaaaaa"}

    items, _ = library_cards(db, q="BBBBBBBBBBB")
    assert [item["video_id"] for item in items] == ["bbbbbbbbbbb"]

    items, _ = library_cards(db, q="chan-beta")
    assert [item["video_id"] for item in items] == ["bbbbbbbbbbb"]

    items, total = library_cards(db, q="no-such-video")
    assert items == [] and total == 0

    items, total = library_cards(db, q="alpha", page=1, per_page=50)
    assert total == 1 and [item["video_id"] for item in items] == ["aaaaaaaaaaa"]


def test_library_files_exports_tags_rebuild_from_disk(tmp_path):
    root = tmp_path / "media-strict"
    _make_video(root, "channel", "aaaaaaaaaaa", "Alpha", {"tags": ["music"]})
    db = tmp_path / "catalog.db"
    assert refresh(root, db) == 1

    files = library_files(db)
    assert any(entry["name"].endswith(".out.mkv") for entry in files)

    links, ids = library_exports(db)
    assert "https://www.youtube.com/watch?v=aaaaaaaaaaa" in links
    assert "video aaaaaaaaaaa" in ids

    tags = library_tags(db)
    assert tags["tags"] == [{"tag": "music", "count": 1}]
    assert tags["videos_by_tag"]["music"][0]["video_id"] == "aaaaaaaaaaa"

    db.unlink()
    assert refresh(root, db) == 1
    assert library_files(db) == files
    assert library_exports(db) == (links, ids)
    assert library_tags(db) == tags


def test_settings_json_seed_and_backup_roundtrip(tmp_path):
    from catalog import (
        backup_settings_to_file,
        read_settings_file,
        seed_settings_from_file,
    )
    db = tmp_path / "catalog.db"
    settings_file = tmp_path / "settings.json"
    set_setting(db, "default_download_mode", "queue")
    set_setting(db, "max_concurrent_downloads", "3")
    backup_settings_to_file(db, settings_file)
    assert read_settings_file(settings_file) == {
        "default_download_mode": "queue", "max_concurrent_downloads": "3"}

    db.unlink()
    assert refresh(tmp_path / "media", db) == 0
    assert setting(db, "default_download_mode", "immediate") == "immediate"
    assert seed_settings_from_file(db, settings_file) == 2
    assert setting(db, "default_download_mode", "immediate") == "queue"
    assert setting(db, "max_concurrent_downloads", "5") == "3"

    # DB wins over the file: existing rows are never overwritten by seeding.
    (tmp_path / "settings2.json").write_text(
        json.dumps({"default_download_mode": "immediate"}), encoding="utf-8")
    assert seed_settings_from_file(db, tmp_path / "settings2.json") == 0
    assert setting(db, "default_download_mode", "immediate") == "queue"

    # Invalid values are ignored, not stored.
    (tmp_path / "bad.json").write_text(
        json.dumps({"default_download_mode": "nope", "max_concurrent_downloads": 99,
                    "unknown_key": "x"}), encoding="utf-8")
    assert read_settings_file(tmp_path / "bad.json") == {}


def test_queue_attempts_settings_are_db_only_not_rebuilt(tmp_path):
    root = tmp_path / "media-strict"
    _make_video(root, "channel", "aaaaaaaaaaa", "Alpha")
    db = tmp_path / "catalog.db"
    assert refresh(root, db) == 1

    queue_add(db, "aaaaaaaaaaa", "video")
    record_attempt(db, "aaaaaaaaaaa", "failed", "boom", "raw", True, 1.0, 2.0)
    set_setting(db, "default_download_mode", "queue")
    assert len(queue_items(db)) == 1
    assert setting(db, "default_download_mode", "immediate") == "queue"

    # Rebuilding from disk restores media rows but not queue/attempts: those
    # tables are primary data with no on-disk mirror. Settings restore from
    # data/settings.json (tested separately); without that file they reset.
    db.unlink()
    assert refresh(root, db) == 1
    items, total = library_cards(db)
    assert total == 1 and items[0]["video_id"] == "aaaaaaaaaaa"
    assert queue_items(db) == []
    assert setting(db, "default_download_mode", "immediate") == "immediate"
    import sqlite3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM download_attempts").fetchone()[0] == 0


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
