"""
HTTP shape contract tests for app3.py via the Flask test client.

No network, no yt-dlp, no ffmpeg. These lock in the slim response shapes
(slim cards, no inlined descriptions/info.json) so a future change cannot
silently re-fatten an endpoint back into a multi-megabyte response.
"""
import json

import pytest

import app3
import catalog


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Isolate all filesystem/DB roots the endpoints read."""
    merged = tmp_path / "merged"
    legacy = tmp_path / "legacy"
    fallback = tmp_path / "fallback"
    for d in (merged, legacy, fallback):
        d.mkdir()
    monkeypatch.setattr(app3, "MERGED_DIR", merged.resolve())
    monkeypatch.setattr(app3, "LEGACY_MERGED_DIR", legacy.resolve())
    monkeypatch.setattr(app3, "FALLBACK_DIR", fallback.resolve())
    monkeypatch.setattr(app3, "CATALOG_DB", (tmp_path / "catalog.db").resolve())
    monkeypatch.setattr(app3, "_LIBRARY_CACHE", {"fingerprint": None, "videos": []})
    return {"merged": merged, "legacy": legacy, "fallback": fallback}


@pytest.fixture()
def client():
    app3.app.config["TESTING"] = True
    return app3.app.test_client()


def _video(root, vid="dQw4w9WgXcQ", channel="UCchannel01", tags=None):
    d = root / channel / vid
    d.mkdir(parents=True)
    (d / f"{channel}.{vid}.20240101.Title [{vid}].out.mkv").write_text("video")
    (d / f"{channel}.{vid}.20240101.Title [{vid}].out.m4a").write_text("audio")
    info = {"id": vid, "title": "Title", "tags": tags or []}
    (d / f"{channel}.{vid}.20240101.Title [{vid}].out.info.json").write_text(json.dumps(info))
    (d / f"{channel}.{vid}.20240101.Title [{vid}].out.description").write_text("lyrics here")
    return d


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_library_returns_slim_cards(client, env):
    _video(env["merged"])

    r = client.get("/api/media/library")
    assert r.status_code == 200
    videos = r.get_json()["videos"]
    assert len(videos) == 1
    card = videos[0]
    assert set(card) == {"video_id", "channel_id", "source_root", "title", "date", "files"}
    assert card["source_root"] == "merged"
    assert card["files"]["video"].startswith("/media/")


def test_library_search_and_pagination(client, env):
    _video(env["merged"], vid="dQw4w9WgXcQ", channel="UCchannel01")
    _video(env["merged"], vid="kIll0-AyMa0", channel="UCchannel02")

    body = client.get("/api/media/library?q=dQw4w9WgXcQ").get_json()
    assert body["total"] == 1
    assert [v["video_id"] for v in body["videos"]] == ["dQw4w9WgXcQ"]

    body = client.get("/api/media/library?q=no-such-video").get_json()
    assert body["videos"] == [] and body["total"] == 0

    body = client.get("/api/media/library?page=1&per_page=1&sort=title").get_json()
    assert body["total"] == 2 and body["pages"] == 2 and len(body["videos"]) == 1

    body = client.get("/api/media/library?page=1&per_page=1&sort=title&q=UCchannel02").get_json()
    assert body["total"] == 1 and body["videos"][0]["video_id"] == "kIll0-AyMa0"


def test_library_search_returns_all_matches_without_explicit_pagination(client, env):
    for index in range(3):
        _video(env["merged"], vid=f"a{index:010d}", channel="UCchannel01")

    body = client.get("/api/media/library?q=UCchannel01").get_json()
    assert body["total"] == 3
    assert len(body["videos"]) == 3


def test_library_supports_descending_title_and_channel_sort(client, env):
    _video(env["merged"], vid="dQw4w9WgXcQ", channel="UCchannel02")
    _video(env["merged"], vid="kIll0-AyMa0", channel="UCchannel01")

    titles = client.get("/api/media/library?sort=title-desc").get_json()["videos"]
    assert [video["video_id"] for video in titles] == ["kIll0-AyMa0", "dQw4w9WgXcQ"]

    channels = client.get("/api/media/library?sort=channel").get_json()["videos"]
    assert [video["video_id"] for video in channels] == ["kIll0-AyMa0", "dQw4w9WgXcQ"]


def test_library_prefers_strict_and_shows_fallback(client, env):
    _video(env["merged"])
    _video(env["fallback"], vid="kIll0-AyMa0")

    videos = {v["video_id"]: v for v in client.get("/api/media/library").get_json()["videos"]}

    assert set(videos) == {"dQw4w9WgXcQ", "kIll0-AyMa0"}
    assert videos["kIll0-AyMa0"]["source_root"] == "bestfallback"
    assert videos["kIll0-AyMa0"]["files"]["video"].startswith("/media-fallback/")


def test_resolve_contract(client, env):
    _video(env["merged"])

    r = client.get("/api/media/resolve/dQw4w9WgXcQ")
    assert r.status_code == 200
    body = r.get_json()
    assert body["result"] is True
    video = body["video"]
    assert video["player_url"].endswith(".out.mkv")
    assert "info_json" not in video["details"]["metadata"]
    assert video["details"]["metadata"]["description"] == "lyrics here"

    assert client.get("/api/media/resolve/AAAAAAAAAAA").status_code == 404
    assert client.get("/api/media/resolve/nope").status_code == 400


def test_resolve_ignores_fallback(client, env):
    _video(env["fallback"], vid="kIll0-AyMa0")

    assert client.get("/api/media/resolve/kIll0-AyMa0").status_code == 404


def test_tags_entries_are_slim(client, env):
    _video(env["merged"], tags=["rock"])

    body = client.get("/api/media/tags").get_json()
    assert [t["tag"] for t in body["tags"]] == ["rock"]
    for entries in body["videos_by_tag"].values():
        for entry in entries:
            assert "details" not in entry


def test_playlist_members_are_slim(client, env):
    _video(env["merged"])
    catalog.record_playlist_membership(
        app3.CATALOG_DB, "PLtestplaylist01", "Test playlist", None,
        [{"video_id": "dQw4w9WgXcQ", "playlist_index": 1, "title": "Title"}],
    )

    r = client.get("/api/media/playlists/PLtestplaylist01")
    assert r.status_code == 200
    members = r.get_json()["videos"]
    assert len(members) == 1
    assert "details" not in members[0]["video"]
    assert members[0]["video"]["files"]["video"].startswith("/media/")


def test_details_has_full_info_json(client, env):
    _video(env["merged"])

    r = client.get("/api/media/details/UCchannel01/dQw4w9WgXcQ")
    assert r.status_code == 200
    info = r.get_json()["metadata"]["info_json"]
    assert info["title"] == "Title"
