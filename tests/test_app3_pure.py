"""
Pure unit tests for app3.py — no Flask test client, no HTTP.

TODO (HTTP / integration tests to add in future):
  - GET /health — fields: ok, archive_exists, active_downloads, etc.
  - GET /api/youtube/<id> — found / not-found / invalid ID / cache hit
  - POST /api/youtube/get/<id> — starts download / already running / max concurrent / invalid
  - GET /api/youtube/status/<id> — idle / downloading / completed / failed / one-time consume
  - POST /api/youtube/playlist/get/<id> — same matrix as video get
  - GET /api/youtube/playlist/status/<id>
  - Rate-limiting: each endpoint returns 429 after its per-minute limit
  - _download_worker thread integration: mock getvidyt, assert result stored correctly
  - _playlist_download_worker thread integration: same pattern
"""
import time
import io
import zipfile
from pathlib import Path

import pytest

import app3
from app3 import (
    _normalize_id,
    _normalize_playlist_id,
    _parse_archive_line,
    _load_ids,
    _ensure_cache,
    _cleanup_old_results,
    _cleanup_old_playlist_results,
    _resolve_media_by_video_id,
    _download_status_snapshot,
    _log_queue_state_locked,
    _RESULT_TTL,
    _progress_hook,
)


@pytest.fixture(autouse=True)
def reset_app3_state(monkeypatch):
    """Reset all module-level mutable state before every test."""
    monkeypatch.setattr(app3, "_cached_mtime", None)
    monkeypatch.setattr(app3, "_cached_ids", set())
    monkeypatch.setattr(app3, "_active_downloads", set())
    monkeypatch.setattr(app3, "_download_started_at", {})
    monkeypatch.setattr(app3, "_download_results", {})
    monkeypatch.setattr(app3, "_result_timestamps", {})
    monkeypatch.setattr(app3, "_download_history", {})
    monkeypatch.setattr(app3, "_download_history_timestamps", {})
    monkeypatch.setattr(app3, "_download_details", {})
    monkeypatch.setattr(app3, "_active_playlist_downloads", set())
    monkeypatch.setattr(app3, "_playlist_started_at", {})
    monkeypatch.setattr(app3, "_playlist_download_results", {})
    monkeypatch.setattr(app3, "_playlist_result_timestamps", {})
    monkeypatch.setattr(app3, "_playlist_history", {})
    monkeypatch.setattr(app3, "_playlist_history_timestamps", {})
    monkeypatch.setattr(app3, "_last_queue_log_message", None)


# ---------------------------------------------------------------------------
# _normalize_id
# ---------------------------------------------------------------------------

class TestNormalizeId:
    def test_valid_id(self):
        assert _normalize_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_strips_surrounding_whitespace(self):
        assert _normalize_id("  dQw4w9WgXcQ  ") == "dQw4w9WgXcQ"

    def test_allows_underscore_and_dash(self):
        # 11 chars: a b c _ d e f - g h i
        assert _normalize_id("abc_def-ghi") == "abc_def-ghi"

    def test_too_short_returns_none(self):
        assert _normalize_id("short") is None

    def test_too_long_returns_none(self):
        assert _normalize_id("A" * 12) is None

    def test_invalid_char_returns_none(self):
        assert _normalize_id("invalid!chars") is None

    def test_empty_string_returns_none(self):
        assert _normalize_id("") is None

    def test_none_coerced_to_empty_returns_none(self):
        # raw or "" handles None gracefully
        assert _normalize_id(None) is None


def test_extension_zip_download_contains_manifest():
    response = app3.app.test_client().get("/extension.zip")

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
        assert "manifest.json" in bundle.namelist()


def test_progress_hook_tracks_each_file_and_overall_bytes():
    hook = _progress_hook("dQw4w9WgXcQ")
    hook({"status": "downloading", "filename": "/tmp/video.mp4", "downloaded_bytes": 50, "total_bytes": 100})
    hook({"status": "finished", "filename": "/tmp/video.mp4", "downloaded_bytes": 100, "total_bytes": 100})
    hook({"status": "downloading", "filename": "/tmp/audio.webm", "downloaded_bytes": 25, "total_bytes": 100})

    detail = app3._download_details["dQw4w9WgXcQ"]
    assert detail["files_completed"] == 1
    assert detail["files_total"] == 2
    assert detail["percent"] == 62.5
    assert detail["files"]["video.mp4"]["status"] == "completed"
    assert detail["files"]["audio.webm"]["percent"] == 25


# ---------------------------------------------------------------------------
# _normalize_playlist_id
# ---------------------------------------------------------------------------

class TestNormalizePlaylistId:
    def test_valid_minimum_length(self):
        assert _normalize_playlist_id("PL") == "PL"

    def test_valid_maximum_length(self):
        pid = "a" * 128
        assert _normalize_playlist_id(pid) == pid

    def test_too_short_returns_none(self):
        assert _normalize_playlist_id("A") is None

    def test_too_long_returns_none(self):
        assert _normalize_playlist_id("a" * 129) is None

    def test_allows_underscores_and_dashes(self):
        assert _normalize_playlist_id("PL_valid-ID") == "PL_valid-ID"

    def test_invalid_char_space_returns_none(self):
        assert _normalize_playlist_id("has space") is None

    def test_empty_returns_none(self):
        assert _normalize_playlist_id("") is None

    def test_strips_whitespace_before_validating(self):
        assert _normalize_playlist_id("  PL  ") == "PL"


# ---------------------------------------------------------------------------
# _parse_archive_line
# ---------------------------------------------------------------------------

class TestParseArchiveLine:
    def test_standard_line(self):
        assert _parse_archive_line("youtube dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_case_insensitive_prefix(self):
        assert _parse_archive_line("YOUTUBE dQw4w9WgXcQ") == "dQw4w9WgXcQ"
        assert _parse_archive_line("YouTube dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_extra_whitespace_between_parts(self):
        assert _parse_archive_line("youtube   dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_blank_line_returns_none(self):
        assert _parse_archive_line("") is None

    def test_whitespace_only_returns_none(self):
        assert _parse_archive_line("   ") is None

    def test_wrong_platform_returns_none(self):
        assert _parse_archive_line("vimeo dQw4w9WgXcQ") is None

    def test_no_id_returns_none(self):
        assert _parse_archive_line("youtube") is None

    def test_leading_trailing_whitespace_on_line(self):
        assert _parse_archive_line("  youtube dQw4w9WgXcQ  ") == "dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# _load_ids
# ---------------------------------------------------------------------------

class TestLoadIds:
    def test_loads_valid_ids(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube dQw4w9WgXcQ\nyoutube abc12345678\n")
        assert _load_ids(f) == {"dQw4w9WgXcQ", "abc12345678"}

    def test_skips_invalid_platform_lines(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("vimeo dQw4w9WgXcQ\nyoutube valid11char\n")
        assert _load_ids(f) == {"valid11char"}

    def test_empty_file_returns_empty_set(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("")
        assert _load_ids(f) == set()

    def test_blank_lines_and_comments_skipped(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("\n\nyoutube AAAAAAAAAAA\n\n")
        assert _load_ids(f) == {"AAAAAAAAAAA"}

    def test_deduplicates_repeated_ids(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube dQw4w9WgXcQ\nyoutube dQw4w9WgXcQ\n")
        assert _load_ids(f) == {"dQw4w9WgXcQ"}


# ---------------------------------------------------------------------------
# _ensure_cache
# ---------------------------------------------------------------------------

class TestEnsureCache:
    def test_missing_file_clears_cache(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app3, "CHECK_FILE", tmp_path / "nonexistent.txt")
        monkeypatch.setattr(app3, "_cached_mtime", 99.0)
        monkeypatch.setattr(app3, "_cached_ids", {"stale"})
        _ensure_cache()
        assert app3._cached_mtime is None
        assert app3._cached_ids == set()

    def test_existing_file_populates_cache(self, monkeypatch, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube dQw4w9WgXcQ\n")
        monkeypatch.setattr(app3, "CHECK_FILE", f)
        _ensure_cache()
        assert "dQw4w9WgXcQ" in app3._cached_ids
        assert app3._cached_mtime == f.stat().st_mtime

    def test_same_mtime_skips_reload(self, monkeypatch, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube dQw4w9WgXcQ\n")
        mtime = f.stat().st_mtime
        monkeypatch.setattr(app3, "CHECK_FILE", f)
        monkeypatch.setattr(app3, "_cached_mtime", mtime)
        monkeypatch.setattr(app3, "_cached_ids", {"sentinel"})
        _ensure_cache()
        # mtime matched — cache must NOT have been reloaded
        assert app3._cached_ids == {"sentinel"}

    def test_changed_mtime_triggers_reload(self, monkeypatch, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube dQw4w9WgXcQ\n")
        monkeypatch.setattr(app3, "CHECK_FILE", f)
        monkeypatch.setattr(app3, "_cached_mtime", 0.0)  # stale mtime
        monkeypatch.setattr(app3, "_cached_ids", {"stale"})
        _ensure_cache()
        assert app3._cached_ids == {"dQw4w9WgXcQ"}


# ---------------------------------------------------------------------------
# _resolve_media_by_video_id
# ---------------------------------------------------------------------------

class TestResolveMediaByVideoId:
    def test_resolves_video_file(self, monkeypatch, tmp_path):
        video_dir = tmp_path / "UCchannel01" / "dQw4w9WgXcQ"
        video_dir.mkdir(parents=True)
        media = video_dir / "UCchannel01.dQw4w9WgXcQ.20240101.Title [dQw4w9WgXcQ].out.mkv"
        media.write_text("video")
        monkeypatch.setattr(app3, "MERGED_DIR", tmp_path.resolve())

        result = _resolve_media_by_video_id("dQw4w9WgXcQ")

        assert result["video_id"] == "dQw4w9WgXcQ"
        assert result["channel_id"] == "UCchannel01"
        assert result["player_kind"] == "video"
        assert result["player_url"].endswith(".out.mkv")

    def test_resolves_audio_when_video_missing(self, monkeypatch, tmp_path):
        video_dir = tmp_path / "UCchannel01" / "abc12345678"
        video_dir.mkdir(parents=True)
        audio = video_dir / "UCchannel01.abc12345678.20240101.Title [abc12345678].out.m4a"
        audio.write_text("audio")
        monkeypatch.setattr(app3, "MERGED_DIR", tmp_path.resolve())

        result = _resolve_media_by_video_id("abc12345678")

        assert result["player_kind"] == "audio"
        assert result["player_url"].endswith(".out.m4a")

    def test_missing_video_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app3, "MERGED_DIR", tmp_path.resolve())

        assert _resolve_media_by_video_id("dQw4w9WgXcQ") is None


# ---------------------------------------------------------------------------
# _download_status_snapshot
# ---------------------------------------------------------------------------

class TestDownloadStatusSnapshot:
    def test_reports_active_and_recent_video_downloads(self):
        now = time.time()
        app3._active_downloads.add("dQw4w9WgXcQ")
        app3._download_started_at["dQw4w9WgXcQ"] = now - 15
        app3._download_history["abc12345678"] = "completed"
        app3._download_history_timestamps["abc12345678"] = now - 5

        snapshot = _download_status_snapshot()

        assert snapshot["ok"] is True
        assert snapshot["counts"]["active_videos"] == 1
        assert snapshot["counts"]["active"] == 1
        assert snapshot["queue"]["empty"] is False
        assert snapshot["queue"]["remaining_videos"] == 1
        assert snapshot["queue"]["message"] == "1 video(s) remaining in queue"
        assert snapshot["active"][0]["id"] == "dQw4w9WgXcQ"
        assert snapshot["active"][0]["status"] == "downloading"
        assert snapshot["recent"][0]["id"] == "abc12345678"
        assert snapshot["recent"][0]["status"] == "completed"

    def test_reports_active_and_recent_playlist_downloads(self):
        now = time.time()
        app3._active_playlist_downloads.add("PLabc123")
        app3._playlist_started_at["PLabc123"] = now - 30
        app3._playlist_history["PLdone"] = "failed"
        app3._playlist_history_timestamps["PLdone"] = now - 6

        snapshot = _download_status_snapshot()

        assert snapshot["counts"]["active_playlists"] == 1
        assert snapshot["active"][0]["kind"] == "playlist"
        assert snapshot["active"][0]["id"] == "PLabc123"
        assert snapshot["recent"][0]["kind"] == "playlist"
        assert snapshot["recent"][0]["status"] == "failed"

    def test_expires_old_recent_results(self):
        now = time.time()
        app3._download_history["oldresult1"] = "completed"
        app3._download_history_timestamps["oldresult1"] = now - (_RESULT_TTL + 1)

        snapshot = _download_status_snapshot()

        assert snapshot["queue"]["empty"] is True
        assert snapshot["queue"]["message"] == "Queue Empty"
        assert snapshot["recent"] == []
        assert "oldresult1" not in app3._download_history

    def test_logs_queue_state_changes(self, monkeypatch):
        messages = []

        def fake_info(message, detail):
            messages.append(message % detail)

        monkeypatch.setattr(app3.app.logger, "info", fake_info)

        with app3._lock:
            app3._active_downloads.add("dQw4w9WgXcQ")
            _log_queue_state_locked()
            _log_queue_state_locked()
            app3._active_downloads.clear()
            _log_queue_state_locked()

        assert messages == [
            "Download queue: 1 video(s) remaining in queue (1 total active item(s), 0 playlist(s))",
            "Download queue: Queue Empty",
        ]


# ---------------------------------------------------------------------------
# _cleanup_old_results
# ---------------------------------------------------------------------------

class TestCleanupOldResults:
    def test_removes_expired_entries(self):
        now = time.time()
        app3._download_results["old"] = "completed"
        app3._result_timestamps["old"] = now - (_RESULT_TTL + 10)
        with app3._lock:
            _cleanup_old_results()
        assert "old" not in app3._download_results
        assert "old" not in app3._result_timestamps

    def test_keeps_fresh_entries(self):
        now = time.time()
        app3._download_results["fresh"] = "failed"
        app3._result_timestamps["fresh"] = now - 10
        with app3._lock:
            _cleanup_old_results()
        assert "fresh" in app3._download_results

    def test_mixed_expired_and_fresh(self):
        now = time.time()
        app3._download_results["old"] = "completed"
        app3._result_timestamps["old"] = now - (_RESULT_TTL + 1)
        app3._download_results["new"] = "failed"
        app3._result_timestamps["new"] = now - 5
        with app3._lock:
            _cleanup_old_results()
        assert "old" not in app3._download_results
        assert "new" in app3._download_results

    def test_empty_dicts_no_error(self):
        with app3._lock:
            _cleanup_old_results()  # must not raise


# ---------------------------------------------------------------------------
# _cleanup_old_playlist_results
# ---------------------------------------------------------------------------

class TestCleanupOldPlaylistResults:
    def test_removes_expired_playlist_entries(self):
        now = time.time()
        app3._playlist_download_results["PLold"] = "completed"
        app3._playlist_result_timestamps["PLold"] = now - (_RESULT_TTL + 10)
        with app3._lock:
            _cleanup_old_playlist_results()
        assert "PLold" not in app3._playlist_download_results

    def test_keeps_fresh_playlist_entries(self):
        now = time.time()
        app3._playlist_download_results["PLnew"] = "failed"
        app3._playlist_result_timestamps["PLnew"] = now - 5
        with app3._lock:
            _cleanup_old_playlist_results()
        assert "PLnew" in app3._playlist_download_results
