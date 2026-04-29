"""
Pure unit tests for getvidyt.py and cli.py — no network, no yt-dlp downloads, no ffmpeg.

TODO (integration tests to add in future):
  - download_youtube() end-to-end: requires network + yt-dlp + real archive file
  - AudioMetadataPostProcessor._embed(): requires ffmpeg subprocess
  - StandaloneAudioMetaPP.process_single() with real audio sidecars: requires ffmpeg
  - StandaloneAudioMetaPP.process_directory(): requires prepared fixture tree
  - build_ydl_opts() with no_js=False: exercises _find_deno_path / Deno runtime config
  - _find_deno_path() common-path fallback: requires creating a file at ~/.deno/bin/deno
  - dihi download / dihi audio-meta: require real yt-dlp download or ffmpeg
"""
import json
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from getvidyt import (
    AudioMetadataPostProcessor,
    MetadataSidecarPostProcessor,
    StandaloneAudioMetaPP,
    _datetime_now,
    _find_deno_path,
    _parse_archive_line,
    _sub_to_text,
    build_ydl_opts,
    load_archive,
    to_youtube_url,
)
from cli import _extract_video_id, _cmd_check


# ---------------------------------------------------------------------------
# _find_deno_path
# ---------------------------------------------------------------------------

class TestFindDenoPath:
    def test_returns_path_from_which(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/deno" if name == "deno" else None)
        assert _find_deno_path() == "/usr/bin/deno"

    def test_falls_back_to_deno_string_when_nothing_found(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        # None of the common hardcoded paths exist in a clean test env
        result = _find_deno_path()
        # Must return a non-empty string; "deno" is the documented fallback
        assert isinstance(result, str)
        assert result  # not empty


# ---------------------------------------------------------------------------
# _datetime_now
# ---------------------------------------------------------------------------

class TestDatetimeNow:
    _ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_returns_iso8601_utc_string(self):
        result = _datetime_now()
        assert self._ISO_RE.match(result), f"Unexpected format: {result!r}"


# ---------------------------------------------------------------------------
# _sub_to_text
# ---------------------------------------------------------------------------

class TestSubToText:
    def test_basic_vtt_extraction(self, tmp_path):
        f = tmp_path / "sub.vtt"
        f.write_text(
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "Hello world\n\n"
            "00:00:04.000 --> 00:00:06.000\n"
            "Goodbye\n"
        )
        assert _sub_to_text(f) == "Hello world\nGoodbye"

    def test_deduplicates_consecutive_identical_lines(self, tmp_path):
        f = tmp_path / "sub.vtt"
        f.write_text(
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "Hello\n\n"
            "00:00:02.000 --> 00:00:03.000\n"
            "Hello\n\n"
            "00:00:03.000 --> 00:00:04.000\n"
            "World\n"
        )
        assert _sub_to_text(f) == "Hello\nWorld"

    def test_strips_html_tags(self, tmp_path):
        f = tmp_path / "sub.vtt"
        f.write_text(
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "<c.colorCCCCCC>Hello</c> <b>world</b>\n"
        )
        assert _sub_to_text(f) == "Hello world"

    def test_srt_format(self, tmp_path):
        f = tmp_path / "sub.srt"
        f.write_text(
            "1\n"
            "00:00:01,000 --> 00:00:03,000\n"
            "Hello SRT world\n"
            "\n"
            "2\n"
            "00:00:04,000 --> 00:00:06,000\n"
            "Second line\n"
        )
        assert _sub_to_text(f) == "Hello SRT world\nSecond line"

    def test_strips_webvtt_metadata_headers(self, tmp_path):
        f = tmp_path / "sub.vtt"
        f.write_text(
            "WEBVTT\n"
            "Kind: captions\n"
            "Language: en\n"
            "\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "Real text\n"
        )
        assert _sub_to_text(f) == "Real text"

    def test_missing_file_returns_none(self, tmp_path):
        assert _sub_to_text(tmp_path / "nonexistent.vtt") is None

    def test_empty_file_returns_none(self, tmp_path):
        f = tmp_path / "empty.vtt"
        f.write_text("")
        assert _sub_to_text(f) is None

    def test_only_headers_and_timestamps_returns_none(self, tmp_path):
        f = tmp_path / "sub.vtt"
        f.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n\n")
        assert _sub_to_text(f) is None

    def test_digit_only_cue_ids_skipped(self, tmp_path):
        f = tmp_path / "sub.vtt"
        f.write_text(
            "WEBVTT\n\n"
            "1\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "Text after cue id\n"
        )
        assert _sub_to_text(f) == "Text after cue id"

    def test_non_consecutive_duplicates_preserved(self, tmp_path):
        f = tmp_path / "sub.vtt"
        f.write_text(
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "A\n\n"
            "00:00:02.000 --> 00:00:03.000\n"
            "B\n\n"
            "00:00:03.000 --> 00:00:04.000\n"
            "A\n"
        )
        # A appears again after B — not consecutive, so both A lines kept
        assert _sub_to_text(f) == "A\nB\nA"


# ---------------------------------------------------------------------------
# to_youtube_url
# ---------------------------------------------------------------------------

class TestToYoutubeUrl:
    def test_bare_video_id(self):
        assert to_youtube_url("dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_bare_playlist_id(self):
        # 16 chars — matches PLAUSIBLE_ID_RE (10+) but not YOUTUBE_VIDEO_ID_RE (11 exact)
        result = to_youtube_url("PLxxxxxxxxxxxx12")
        assert result == "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxx12"

    def test_full_https_url_unchanged(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert to_youtube_url(url) == url

    def test_www_prefix_gets_https(self):
        result = to_youtube_url("www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_strips_whitespace(self):
        assert to_youtube_url("  dQw4w9WgXcQ  ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_unrecognised_short_string_returned_as_is(self):
        # 5 chars — doesn't match either ID regex
        assert to_youtube_url("short") == "short"

    def test_ten_char_alphanumeric_becomes_playlist(self):
        # Exactly 10 chars matches PLAUSIBLE_ID_RE but not YOUTUBE_VIDEO_ID_RE
        result = to_youtube_url("PLxxxxxxxx")
        assert "playlist?list=" in result


# ---------------------------------------------------------------------------
# AudioMetadataPostProcessor._clean_filename
# ---------------------------------------------------------------------------

class TestAudioMetadataCleanFilename:
    def test_strips_m4a_format_id(self):
        src = Path("/some/dir/Title [id].out.f140.m4a")
        assert AudioMetadataPostProcessor._clean_filename(src).name == "Title [id].out.m4a"

    def test_strips_webm_format_id(self):
        src = Path("/some/dir/Title [id].out.f251.webm")
        assert AudioMetadataPostProcessor._clean_filename(src).name == "Title [id].out.webm"

    def test_preserves_dots_in_title(self):
        src = Path("/some/dir/A.B.C [id].out.f140.m4a")
        assert AudioMetadataPostProcessor._clean_filename(src).name == "A.B.C [id].out.m4a"

    def test_no_format_id_unchanged(self):
        src = Path("/some/dir/Title [id].out.m4a")
        assert AudioMetadataPostProcessor._clean_filename(src).name == "Title [id].out.m4a"

    def test_preserves_parent_directory(self):
        src = Path("/some/dir/Title [id].out.f140.m4a")
        result = AudioMetadataPostProcessor._clean_filename(src)
        assert result.parent == Path("/some/dir")


# ---------------------------------------------------------------------------
# AudioMetadataPostProcessor._find_subtitle
# ---------------------------------------------------------------------------

class TestAudioMetadataFindSubtitle:
    def test_returns_subtitle_from_info_dict(self, tmp_path):
        sub = tmp_path / "video.en.vtt"
        sub.write_text("WEBVTT\n")
        info = {"requested_subtitles": {"en": {"filepath": str(sub), "ext": "vtt"}}}
        result = AudioMetadataPostProcessor._find_subtitle(info, tmp_path / "video.mkv")
        assert result == sub

    def test_missing_filepath_falls_back_to_glob(self, tmp_path):
        sub = tmp_path / "video.en.vtt"
        sub.write_text("WEBVTT\n")
        # info points to a nonexistent path — must fall back to glob
        info = {"requested_subtitles": {"en": {"filepath": "/nonexistent.vtt", "ext": "vtt"}}}
        result = AudioMetadataPostProcessor._find_subtitle(info, tmp_path / "video.mkv")
        assert result == sub

    def test_prefers_vtt_over_srt_in_glob(self, tmp_path):
        (tmp_path / "video.en.vtt").write_text("WEBVTT\n")
        (tmp_path / "video.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
        result = AudioMetadataPostProcessor._find_subtitle({}, tmp_path / "video.mkv")
        assert result is not None
        assert result.suffix == ".vtt"

    def test_returns_none_when_no_subtitles(self, tmp_path):
        result = AudioMetadataPostProcessor._find_subtitle({}, tmp_path / "video.mkv")
        assert result is None

    def test_skips_wrong_ext_in_info(self, tmp_path):
        # ext is "json" — not vtt/srt — should not be returned
        fake = tmp_path / "video.info.json"
        fake.write_text("{}")
        info = {"requested_subtitles": {"en": {"filepath": str(fake), "ext": "json"}}}
        result = AudioMetadataPostProcessor._find_subtitle(info, tmp_path / "video.mkv")
        assert result is None


# ---------------------------------------------------------------------------
# AudioMetadataPostProcessor._find_thumbnail
# ---------------------------------------------------------------------------

class TestAudioMetadataFindThumbnail:
    def test_returns_thumbnail_from_info_dict(self, tmp_path):
        thumb = tmp_path / "video.png"
        thumb.write_bytes(b"PNG")
        info = {"thumbnails": [{"filepath": str(thumb)}]}
        result = AudioMetadataPostProcessor._find_thumbnail(info, tmp_path / "video.mkv")
        assert result == thumb

    def test_falls_back_to_glob_png(self, tmp_path):
        thumb = tmp_path / "video.png"
        thumb.write_bytes(b"PNG")
        result = AudioMetadataPostProcessor._find_thumbnail({}, tmp_path / "video.mkv")
        assert result == thumb

    def test_falls_back_to_glob_jpg(self, tmp_path):
        thumb = tmp_path / "video.jpg"
        thumb.write_bytes(b"JPEG")
        result = AudioMetadataPostProcessor._find_thumbnail({}, tmp_path / "video.mkv")
        assert result == thumb

    def test_returns_none_when_no_thumbnail(self, tmp_path):
        result = AudioMetadataPostProcessor._find_thumbnail({}, tmp_path / "video.mkv")
        assert result is None

    def test_skips_nonexistent_filepath_in_info(self, tmp_path):
        info = {"thumbnails": [{"filepath": "/nonexistent/thumb.png"}]}
        result = AudioMetadataPostProcessor._find_thumbnail(info, tmp_path / "video.mkv")
        assert result is None


# ---------------------------------------------------------------------------
# AudioMetadataPostProcessor._find_audio_files
# ---------------------------------------------------------------------------

class TestAudioMetadataFindAudioFiles:
    def test_returns_matching_audio_sidecar(self, tmp_path):
        sidecar = tmp_path / "video.out.f140.m4a"
        sidecar.write_bytes(b"audio")
        final = tmp_path / "video.out.mkv"
        info = {
            "requested_formats": [
                {"format_id": "140", "acodec": "mp4a.40.2", "vcodec": "none"},
                {"format_id": "271", "acodec": "none", "vcodec": "vp9"},
            ]
        }
        pp = AudioMetadataPostProcessor()
        result = pp._find_audio_files(info, final)
        assert sidecar in result

    def test_excludes_video_only_format(self, tmp_path):
        (tmp_path / "video.out.f271.webm").write_bytes(b"video")
        final = tmp_path / "video.out.mkv"
        info = {
            "requested_formats": [
                {"format_id": "271", "acodec": "none", "vcodec": "vp9"},
            ]
        }
        pp = AudioMetadataPostProcessor()
        assert pp._find_audio_files(info, final) == []

    def test_empty_when_no_requested_formats(self, tmp_path):
        final = tmp_path / "video.out.mkv"
        pp = AudioMetadataPostProcessor()
        assert pp._find_audio_files({}, final) == []

    def test_excludes_sidecar_whose_format_id_not_in_audio_fids(self, tmp_path):
        # File exists but format_id 999 is not in the audio-only set
        (tmp_path / "video.out.f999.m4a").write_bytes(b"audio")
        final = tmp_path / "video.out.mkv"
        info = {
            "requested_formats": [
                {"format_id": "140", "acodec": "mp4a.40.2", "vcodec": "none"},
            ]
        }
        pp = AudioMetadataPostProcessor()
        # format 140 sidecar doesn't exist on disk, 999 exists but isn't in audio_fids for 140
        assert pp._find_audio_files(info, final) == []


# ---------------------------------------------------------------------------
# MetadataSidecarPostProcessor._append_if_changed
# ---------------------------------------------------------------------------

class TestMetadataSidecarAppendIfChanged:
    def test_first_write_creates_file(self, tmp_path):
        path = tmp_path / ".channel_name"
        MetadataSidecarPostProcessor._append_if_changed(path, "My Channel", "2026-01-01T00:00:00Z")
        assert path.read_text() == "2026-01-01T00:00:00Z My Channel\n"

    def test_same_value_not_duplicated(self, tmp_path):
        path = tmp_path / ".channel_name"
        MetadataSidecarPostProcessor._append_if_changed(path, "My Channel", "2026-01-01T00:00:00Z")
        MetadataSidecarPostProcessor._append_if_changed(path, "My Channel", "2026-02-01T00:00:00Z")
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1

    def test_changed_value_appends_new_line(self, tmp_path):
        path = tmp_path / ".channel_name"
        MetadataSidecarPostProcessor._append_if_changed(path, "Old Name", "2026-01-01T00:00:00Z")
        MetadataSidecarPostProcessor._append_if_changed(path, "New Name", "2026-02-01T00:00:00Z")
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        assert "Old Name" in lines[0]
        assert "New Name" in lines[1]

    def test_none_value_is_no_op(self, tmp_path):
        path = tmp_path / ".channel_name"
        MetadataSidecarPostProcessor._append_if_changed(path, None, "2026-01-01T00:00:00Z")
        assert not path.exists()

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "deep" / ".channel_name"
        MetadataSidecarPostProcessor._append_if_changed(path, "Chan", "2026-01-01T00:00:00Z")
        assert path.exists()

    def test_empty_string_value_is_no_op(self, tmp_path):
        path = tmp_path / ".channel_name"
        MetadataSidecarPostProcessor._append_if_changed(path, "", "2026-01-01T00:00:00Z")
        assert not path.exists()


# ---------------------------------------------------------------------------
# MetadataSidecarPostProcessor.run
# ---------------------------------------------------------------------------

class TestMetadataSidecarRun:
    def test_writes_all_sidecar_files(self, tmp_path):
        merged_dir = tmp_path / "merged"
        channel_dir = merged_dir / "UCxxx"
        video_dir = channel_dir / "dQw4w9WgXcQ"
        video_dir.mkdir(parents=True)
        video_file = video_dir / "20090101.Never Gonna Give You Up [dQw4w9WgXcQ].out.mkv"
        video_file.write_bytes(b"")

        pp = MetadataSidecarPostProcessor(merged_dir)
        info = {
            "channel_id": "UCxxx",
            "filepath": str(video_file),
            "channel": "Rick Astley",
            "uploader": "RickAstleyVEVO",
            "uploader_id": "@RickAstley",
            "title": "Never Gonna Give You Up",
            "upload_date": "20090101",
        }
        files_to_delete, returned_info = pp.run(info)

        assert files_to_delete == []
        assert returned_info is info
        assert (channel_dir / ".channel_name").exists()
        assert (channel_dir / ".uploader_id").exists()
        assert (channel_dir / ".uploader_name").exists()
        assert (video_dir / ".title_name").exists()
        assert (video_dir / ".upload_date").exists()

    def test_run_without_channel_id_skips_channel_files(self, tmp_path):
        merged_dir = tmp_path / "merged"
        video_dir = merged_dir / "vid"
        video_dir.mkdir(parents=True)
        video_file = video_dir / "video.mkv"
        video_file.write_bytes(b"")

        pp = MetadataSidecarPostProcessor(merged_dir)
        info = {
            "filepath": str(video_file),
            "title": "Some Video",
            "upload_date": "20260101",
        }
        pp.run(info)
        # No channel dir should have been created
        assert not (merged_dir / "None").exists()

    def test_run_without_filepath_skips_video_files(self, tmp_path):
        merged_dir = tmp_path / "merged"
        channel_dir = merged_dir / "UCyyy"
        channel_dir.mkdir(parents=True)

        pp = MetadataSidecarPostProcessor(merged_dir)
        info = {
            "channel_id": "UCyyy",
            "channel": "Some Channel",
        }
        pp.run(info)
        assert (channel_dir / ".channel_name").exists()
        # No video-level files without a filepath
        assert not (channel_dir / ".title_name").exists()


# ---------------------------------------------------------------------------
# build_ydl_opts
# ---------------------------------------------------------------------------

class TestBuildYdlOpts:
    def test_returns_expected_keys(self, tmp_path):
        opts = build_ydl_opts(
            merged_dir=tmp_path / "merged",
            archive=tmp_path / "archive.txt",
            no_js=True,
        )
        for key in ("format", "outtmpl", "merge_output_format", "keepvideo",
                    "writeinfojson", "writedescription", "writethumbnail",
                    "download_archive", "postprocessors"):
            assert key in opts, f"Missing key: {key}"

    def test_keepvideo_is_true(self, tmp_path):
        opts = build_ydl_opts(
            merged_dir=tmp_path / "merged",
            archive=tmp_path / "archive.txt",
            no_js=True,
        )
        assert opts["keepvideo"] is True

    def test_no_js_excludes_js_runtimes(self, tmp_path):
        opts = build_ydl_opts(
            merged_dir=tmp_path / "merged",
            archive=tmp_path / "archive.txt",
            no_js=True,
        )
        assert "js_runtimes" not in opts
        assert "remote_components" not in opts

    def test_cookies_browser_sets_cookiesfrombrowser(self, tmp_path):
        opts = build_ydl_opts(
            merged_dir=tmp_path / "merged",
            archive=tmp_path / "archive.txt",
            no_js=True,
            cookies_browser="firefox",
        )
        assert opts["cookiesfrombrowser"] == ("firefox",)

    def test_cookies_file_present_sets_cookiefile(self, tmp_path):
        (tmp_path / "cookies.txt").write_text("# Netscape HTTP Cookie File\n")
        opts = build_ydl_opts(
            merged_dir=tmp_path / "merged",
            archive=tmp_path / "archive.txt",
            no_js=True,
        )
        assert opts.get("cookiefile") == str(tmp_path / "cookies.txt")

    def test_cookies_file_absent_no_cookiefile_key(self, tmp_path):
        opts = build_ydl_opts(
            merged_dir=tmp_path / "merged",
            archive=tmp_path / "archive.txt",
            no_js=True,
        )
        assert "cookiefile" not in opts

    def test_extra_opts_override_defaults(self, tmp_path):
        opts = build_ydl_opts(
            merged_dir=tmp_path / "merged",
            archive=tmp_path / "archive.txt",
            no_js=True,
            extra_opts={"format": "bestaudio", "quiet": True},
        )
        assert opts["format"] == "bestaudio"
        assert opts["quiet"] is True

    def test_creates_merged_dir(self, tmp_path):
        merged = tmp_path / "merged"
        assert not merged.exists()
        build_ydl_opts(merged_dir=merged, archive=tmp_path / "archive.txt", no_js=True)
        assert merged.is_dir()


# ---------------------------------------------------------------------------
# StandaloneAudioMetaPP.from_info_json
# ---------------------------------------------------------------------------

class TestStandaloneAudioMetaFromInfoJson:
    def _write_info_json(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_resolved_true_when_mkv_exists(self, tmp_path):
        info_data = {"id": "dQw4w9WgXcQ", "title": "Test", "thumbnails": []}
        info_json = tmp_path / "video.out.info.json"
        self._write_info_json(info_json, info_data)
        (tmp_path / "video.out.mkv").write_bytes(b"")

        info, resolved = StandaloneAudioMetaPP.from_info_json(info_json)
        assert resolved is True
        assert "filepath" in info
        assert info["id"] == "dQw4w9WgXcQ"

    def test_resolved_false_when_no_merged_file(self, tmp_path):
        info_json = tmp_path / "video.out.info.json"
        self._write_info_json(info_json, {"id": "abc12345678", "title": "No merge"})

        _info, resolved = StandaloneAudioMetaPP.from_info_json(info_json)
        assert resolved is False

    def test_raises_for_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            StandaloneAudioMetaPP.from_info_json(tmp_path / "nonexistent.info.json")

    def test_thumbnail_filepath_resolved_from_disk(self, tmp_path):
        thumb = tmp_path / "video.out.png"
        thumb.write_bytes(b"PNG")
        (tmp_path / "video.out.mkv").write_bytes(b"")
        info_data = {
            "id": "abc12345678",
            "title": "T",
            "thumbnails": [{"url": "https://example.com/thumb.jpg"}],
        }
        info_json = tmp_path / "video.out.info.json"
        self._write_info_json(info_json, info_data)

        info, resolved = StandaloneAudioMetaPP.from_info_json(info_json)
        assert resolved is True
        # The thumb on disk should have been wired into thumbnails[0]
        assert info["thumbnails"][0].get("filepath") == str(thumb)


# ---------------------------------------------------------------------------
# StandaloneAudioMetaPP.process_single
# ---------------------------------------------------------------------------

class TestStandaloneAudioMetaProcessSingle:
    def test_returns_false_when_no_merged_file(self, tmp_path):
        info_json = tmp_path / "video.out.info.json"
        info_json.write_text(json.dumps({"id": "abc12345678", "title": "T"}))
        assert StandaloneAudioMetaPP.process_single(info_json) is False

    def test_returns_true_when_merged_file_exists_no_sidecars(self, tmp_path):
        # No audio sidecars → _find_audio_files returns [] → run() exits early
        # but process_single still returns True (it ran successfully, nothing to embed)
        info_json = tmp_path / "video.out.info.json"
        info_json.write_text(json.dumps({"id": "abc12345678", "title": "T", "thumbnails": []}))
        (tmp_path / "video.out.mkv").write_bytes(b"")
        assert StandaloneAudioMetaPP.process_single(info_json) is True


# ---------------------------------------------------------------------------
# _parse_archive_line (now in getvidyt.py, also used by load_archive)
# ---------------------------------------------------------------------------

class TestParseArchiveLineGetvidyt:
    def test_valid_line(self):
        assert _parse_archive_line("youtube dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_case_insensitive(self):
        assert _parse_archive_line("YOUTUBE abc12345678") == "abc12345678"

    def test_blank_returns_none(self):
        assert _parse_archive_line("") is None

    def test_wrong_platform_returns_none(self):
        assert _parse_archive_line("vimeo dQw4w9WgXcQ") is None

    def test_no_id_returns_none(self):
        assert _parse_archive_line("youtube") is None


# ---------------------------------------------------------------------------
# load_archive
# ---------------------------------------------------------------------------

class TestLoadArchive:
    def test_returns_ids_from_file(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube dQw4w9WgXcQ\nyoutube abc12345678\n")
        ids = load_archive(f)
        assert ids == {"dQw4w9WgXcQ", "abc12345678"}

    def test_missing_file_returns_empty_set(self, tmp_path):
        ids = load_archive(tmp_path / "nonexistent.txt")
        assert ids == set()

    def test_ignores_non_youtube_lines(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("vimeo abc123\nyoutube validID123\n")
        ids = load_archive(f)
        assert ids == {"validID123"}

    def test_empty_file_returns_empty_set(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("")
        assert load_archive(f) == set()

    def test_deduplicates(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube dQw4w9WgXcQ\nyoutube dQw4w9WgXcQ\n")
        assert load_archive(f) == {"dQw4w9WgXcQ"}


# ---------------------------------------------------------------------------
# cli._extract_video_id
# ---------------------------------------------------------------------------

class TestExtractVideoId:
    def test_bare_video_id(self):
        assert _extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_full_watch_url(self):
        assert _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_url_with_extra_params(self):
        assert _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42") == "dQw4w9WgXcQ"

    def test_playlist_id_returns_none(self):
        # Playlist IDs are not 11 chars and have no ?v= param
        assert _extract_video_id("PLxxxxxxxxxxxxxx") is None

    def test_empty_string_returns_none(self):
        assert _extract_video_id("") is None

    def test_strips_whitespace(self):
        assert _extract_video_id("  dQw4w9WgXcQ  ") == "dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# cli._cmd_check
# ---------------------------------------------------------------------------

class TestCliCheck:
    def _make_args(self, target: str, archive: Path):
        import argparse
        ns = argparse.Namespace(target=target, archive=str(archive))
        return ns

    def test_found_returns_0(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube dQw4w9WgXcQ\n")
        rc = _cmd_check(self._make_args("dQw4w9WgXcQ", f))
        assert rc == 0

    def test_not_found_returns_1(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube AAAAAAAAAAA\n")
        rc = _cmd_check(self._make_args("dQw4w9WgXcQ", f))
        assert rc == 1

    def test_url_lookup_found(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("youtube dQw4w9WgXcQ\n")
        rc = _cmd_check(self._make_args("https://www.youtube.com/watch?v=dQw4w9WgXcQ", f))
        assert rc == 0

    def test_missing_archive_returns_1(self, tmp_path):
        rc = _cmd_check(self._make_args("dQw4w9WgXcQ", tmp_path / "nonexistent.txt"))
        assert rc == 1

    def test_unrecognised_target_returns_2(self, tmp_path):
        f = tmp_path / "archive.txt"
        f.write_text("")
        rc = _cmd_check(self._make_args("not-an-id", f))
        assert rc == 2
