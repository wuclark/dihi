#!/usr/bin/env python3
from __future__ import annotations

import io
import mimetypes
import json
import os
import re
import sqlite3
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional, Set
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("text/vtt", ".vtt")
mimetypes.add_type("audio/opus", ".opus")

import getvidyt  # must be importable in this environment
import catalog

app = Flask(__name__)
CORS(app)  # Allow all origins

# Rate limiting per IP address
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["100 per minute"],
    storage_uri="memory://",
)

# Validate YouTube video IDs (11 chars: alphanumeric, underscore, dash)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Validate YouTube playlist IDs (alphanumeric, underscore, dash, 2-128 chars)
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,128}$")

# Max concurrent downloads to prevent resource exhaustion
MAX_CONCURRENT_DOWNLOADS = 5
MAX_CONCURRENT_PLAYLIST_DOWNLOADS = 2

# Archive lines look like: "youtube <id>"
CHECK_FILE = Path("./archive.txt").expanduser().resolve()
MERGED_DIR = Path("./merged").expanduser().resolve()
LEGACY_MERGED_DIR = Path("./data/merged").expanduser().resolve()
FALLBACK_DIR = Path("./data/bestfallback").expanduser().resolve()
CATALOG_DB = Path(os.environ.get("DIHI_CATALOG_DB", "./data/media-catalog.db")).expanduser().resolve()
_APP_DIR = Path(__file__).resolve().parent
_SOURCE_EXTENSION_DIR = _APP_DIR / "extension"
for _parent in _APP_DIR.parents:
    candidate = _parent / "extension"
    if candidate.is_dir():
        _SOURCE_EXTENSION_DIR = candidate
        break
EXTENSION_DIR = Path(os.environ.get("DIHI_EXTENSION_DIR", _SOURCE_EXTENSION_DIR))

# Matches format sidecar files, e.g. Title [id].out.f140.m4a
_SIDECAR_RE = re.compile(r"\.f\d+\.[^.]+$")
# Parses the standard output filename to extract metadata
_FNAME_META_RE = re.compile(
    r"^(?:(?P<channel_id>[^.]+)\.(?P<prefix_vid>[A-Za-z0-9_-]{11})\.)?"
    r"(?P<date>\d{8})\.(?P<title>.+?)\s\[(?P<vid>[A-Za-z0-9_-]{11})\]\.out\."
)

_lock = threading.Lock()
_cached_mtime: Optional[float] = None
_cached_ids: Set[str] = set()

_active_downloads: Set[str] = set()  # prevent spamming duplicate downloads
_download_started_at: dict[str, float] = {}
_download_results: dict[str, str] = {}  # video_id -> "completed" | "failed"
_RESULT_TTL = 300  # Keep results for 5 minutes
_result_timestamps: dict[str, float] = {}
_download_history: dict[str, str] = {}
_download_history_timestamps: dict[str, float] = {}
_download_details: dict[str, dict] = {}

# Playlist download tracking
_active_playlist_downloads: Set[str] = set()
_playlist_started_at: dict[str, float] = {}
_playlist_download_results: dict[str, str] = {}  # playlist_id -> "completed" | "failed"
_playlist_result_timestamps: dict[str, float] = {}
_playlist_history: dict[str, str] = {}
_playlist_history_timestamps: dict[str, float] = {}
_last_queue_log_message: Optional[str] = None
_queue_wakeup = threading.Event()


def _refresh_catalog() -> None:
    try:
        count = catalog.refresh([MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR], CATALOG_DB, CHECK_FILE)
        app.logger.info("Media catalog indexed %d videos", count)
    except Exception:
        app.logger.exception("Media catalog refresh failed")


threading.Thread(target=_refresh_catalog, name="media-catalog", daemon=True).start()


def _normalize_id(raw: str) -> Optional[str]:
    """Normalize and validate YouTube video ID."""
    vid = (raw or "").strip()
    if not vid or not YOUTUBE_ID_RE.match(vid):
        return None
    return vid


def _normalize_playlist_id(raw: str) -> Optional[str]:
    """Normalize and validate YouTube playlist ID."""
    pid = (raw or "").strip()
    if not pid or not PLAYLIST_ID_RE.match(pid):
        return None
    return pid


def _parse_archive_line(line: str) -> Optional[str]:
    s = line.strip()
    if not s:
        return None

    parts = s.split()
    if len(parts) < 2:
        return None

    if parts[0].strip().lower() != "youtube":
        return None

    vid = parts[1].strip()
    return vid or None


def _load_ids(path: Path) -> Set[str]:
    ids: Set[str] = set()
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            vid = _parse_archive_line(line)
            if vid:
                ids.add(vid)
    return ids


def _ensure_cache() -> None:
    global _cached_mtime, _cached_ids

    if not CHECK_FILE.exists():
        with _lock:
            _cached_mtime = None
            _cached_ids = set()
        return

    mtime = CHECK_FILE.stat().st_mtime
    with _lock:
        if _cached_mtime == mtime:
            return
        _cached_ids = _load_ids(CHECK_FILE)
        _cached_mtime = mtime


def _cleanup_old_results() -> None:
    """Remove download results older than TTL. Must be called with _lock held."""
    now = time.time()
    expired = [vid for vid, ts in _result_timestamps.items() if now - ts > _RESULT_TTL]
    for vid in expired:
        _download_results.pop(vid, None)
        _result_timestamps.pop(vid, None)


def _cleanup_old_download_history() -> None:
    """Remove download page history older than TTL. Must be called with _lock held."""
    now = time.time()
    expired = [
        vid for vid, ts in _download_history_timestamps.items() if now - ts > _RESULT_TTL
    ]
    for vid in expired:
        _download_history.pop(vid, None)
        _download_history_timestamps.pop(vid, None)


def _format_download_entries(
    active_ids: Set[str],
    started_at: dict[str, float],
    results: dict[str, str],
    result_timestamps: dict[str, float],
    kind: str,
    now: float,
) -> list[dict]:
    entries = []
    for item_id in sorted(active_ids):
        started = started_at.get(item_id)
        entries.append(
            {
                "id": item_id,
                "kind": kind,
                "status": "downloading",
                "active": True,
                "started_at": started,
                "elapsed_seconds": round(now - started, 1) if started else None,
                "finished_at": None,
                "age_seconds": None,
                **_download_details.get(item_id, {}),
            }
        )
    for item_id, result in sorted(results.items()):
        if item_id in active_ids:
            continue
        finished = result_timestamps.get(item_id)
        entries.append(
            {
                "id": item_id,
                "kind": kind,
                "status": result,
                "active": False,
                "started_at": started_at.get(item_id),
                "elapsed_seconds": None,
                "finished_at": finished,
                "age_seconds": round(now - finished, 1) if finished else None,
                **_download_details.get(item_id, {}),
            }
        )
    return entries


def _progress_hook(video_id: str):
    """Create a yt-dlp hook that keeps concise progress text for the UI."""
    def hook(event: dict) -> None:
        status = event.get("status", "")
        info = event.get("info_dict") or {}
        entry_id = str(info.get("id") or "").strip()
        entry_title = str(info.get("title") or entry_id).strip()
        entry_index = info.get("playlist_index")
        entry_total = info.get("n_entries") or info.get("playlist_count")
        filename = Path(event.get("filename", "")).name if event.get("filename") else ""
        total = event.get("total_bytes") or event.get("total_bytes_estimate")
        downloaded = event.get("downloaded_bytes") or 0
        percent = round(downloaded * 100 / total, 1) if total else None
        phase = "finished" if status == "finished" else status or "working"
        if filename:
            phase = f"{phase}: {filename}"
        if percent is not None:
            phase += f" ({percent:g}%)"
        with _lock:
            detail = _download_details.setdefault(video_id, {"logs": []})
            if entry_id:
                playlist_items = detail.setdefault("playlist_items", {})
                item = playlist_items.setdefault(entry_id, {"id": entry_id, "title": entry_title})
                item.update({
                    "id": entry_id,
                    "title": entry_title,
                    "index": entry_index if entry_index is not None else item.get("index"),
                    "total": entry_total if entry_total is not None else item.get("total"),
                    "status": "completed" if status == "finished" else "downloading",
                    "percent": 100 if status == "finished" else percent,
                })
                if entry_index is not None:
                    detail["playlist_index"] = entry_index
                if entry_total:
                    detail["playlist_total"] = entry_total
            files = detail.setdefault("files", {})
            file_state = files.setdefault(filename or "current", {"filename": filename})
            file_state.update({
                "filename": filename,
                "status": "completed" if status == "finished" else "downloading",
                "percent": 100 if status == "finished" else percent,
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "phase": phase,
            })
            if entry_id:
                detail["playlist_items"][entry_id].setdefault("files", {})[filename or "current"] = file_state
            known = [item for item in files.values() if item.get("total_bytes")]
            total_bytes = sum(item["total_bytes"] for item in known)
            downloaded_bytes = sum(
                min(item.get("downloaded_bytes") or 0, item["total_bytes"])
                for item in known
            )
            detail.update({
                "phase": phase,
                "filename": filename,
                "percent": round(downloaded_bytes * 100 / total_bytes, 1) if total_bytes else None,
                "files_completed": sum(item.get("status") == "completed" for item in files.values()),
                "files_total": len(files),
            })
            logs = detail.setdefault("logs", [])
            if not logs or logs[-1] != phase:
                logs.append(phase)
                del logs[:-40]
    return hook


def _classify_download_error(message: str) -> tuple[str, bool]:
    text = (message or "").lower()
    patterns = [
        ("private", "private", False), ("age", "age_restricted", True),
        ("members only", "members_only", False), ("not available in your country", "region_blocked", False),
        ("format is not available", "format_unavailable", True), ("403", "http_403", True),
        ("video unavailable", "not_found", False), ("sign in", "login_required", True),
        ("did not produce both", "incomplete", True),
    ]
    for needle, reason, retryable in patterns:
        if needle in text:
            return reason, retryable
    return "unknown", True


def _queue_summary_from_active(active_videos: int, active_playlists: int) -> dict:
    remaining_total = active_videos + active_playlists
    queue_empty = remaining_total == 0
    return {
        "empty": queue_empty,
        "message": "Queue Empty" if queue_empty else f"{active_videos} video(s) remaining in queue",
        "remaining_videos": active_videos,
        "remaining_playlists": active_playlists,
        "remaining_total": remaining_total,
    }


def _log_queue_state_locked() -> None:
    """Log queue state changes. Must be called with _lock held."""
    global _last_queue_log_message
    queue = _queue_summary_from_active(
        len(_active_downloads),
        len(_active_playlist_downloads),
    )
    detail = (
        queue["message"]
        if queue["empty"]
        else (
            f"{queue['message']} "
            f"({queue['remaining_total']} total active item(s), "
            f"{queue['remaining_playlists']} playlist(s))"
        )
    )
    if detail == _last_queue_log_message:
        return
    _last_queue_log_message = detail
    app.logger.info("Download queue: %s", detail)


def _download_status_snapshot() -> dict:
    now = time.time()
    with _lock:
        _cleanup_old_results()
        _cleanup_old_playlist_results()
        _cleanup_old_download_history()
        _cleanup_old_playlist_history()
        videos = _format_download_entries(
            _active_downloads,
            _download_started_at,
            _download_history,
            _download_history_timestamps,
            "video",
            now,
        )
        playlists = _format_download_entries(
            _active_playlist_downloads,
            _playlist_started_at,
            _playlist_history,
            _playlist_history_timestamps,
            "playlist",
            now,
        )

    active = [entry for entry in [*videos, *playlists] if entry["active"]]
    recent = [entry for entry in [*videos, *playlists] if not entry["active"]]
    active_videos = [entry for entry in videos if entry["active"]]
    active_playlists = [entry for entry in playlists if entry["active"]]
    remaining_videos = len(active_videos)
    queue = _queue_summary_from_active(remaining_videos, len(active_playlists))
    return {
        "ok": True,
        "active": active,
        "recent": recent,
        "videos": videos,
        "playlists": playlists,
        "queue": queue,
        "counts": {
            "active": len(active),
            "recent": len(recent),
            "active_videos": remaining_videos,
            "active_playlists": len(active_playlists),
            "max_videos": MAX_CONCURRENT_DOWNLOADS,
            "max_playlists": MAX_CONCURRENT_PLAYLIST_DOWNLOADS,
        },
        "result_ttl_seconds": _RESULT_TTL,
    }


def _download_worker(video_id: str, cookies_browser: str | None = None, queue_item_id: int | None = None) -> None:
    """
    Actually runs:
      getvidyt.download_youtube(video_id, audio_meta=True)
    Tracks completion status for proper UI feedback.
    """
    success = False
    error_text = ""
    started_at = time.time()
    try:
        result_code = getvidyt.download_youtube(
            video_id,
            audio_meta=True,
            cookies_browser=cookies_browser,
            extra_opts={"progress_hooks": [_progress_hook(video_id)]},
        )
        # Give filesystem time to sync archive.txt
        time.sleep(0.5)
        # Force cache refresh and check if video is now in archive
        global _cached_mtime
        with _lock:
            _cached_mtime = None  # Force refresh
        _ensure_cache()
        media = _resolve_media_by_video_id(video_id) or {}
        media_files = media.get("files") or {}
        has_video = bool(media_files.get("video"))
        has_audio = bool(media_files.get("audio"))
        with _lock:
            success = result_code == 0 and video_id in _cached_ids and has_video and has_audio
        if result_code:
            error_text = f"yt-dlp returned exit code {result_code}"
        elif not success:
            error_text = "download did not produce both a playable video and audio file"
    except Exception as e:
        error_text = str(e)
        app.logger.exception("Download failed for %s: %s", video_id, e)
        success = False
    finally:
        with _lock:
            _active_downloads.discard(video_id)
            # Store result for status endpoint
            result = "completed" if success else "failed"
            finished_at = time.time()
            _download_results[video_id] = result
            _result_timestamps[video_id] = finished_at
            _download_history[video_id] = result
            _download_history_timestamps[video_id] = finished_at
            _download_details.setdefault(video_id, {}).update(
                {"phase": result, "percent": 100 if success else None}
            )
            # Cleanup old results
            _cleanup_old_results()
            _cleanup_old_download_history()
            _log_queue_state_locked()
        if success:
            _refresh_catalog()
            catalog.record_attempt(CATALOG_DB, video_id, "completed", "authenticated" if cookies_browser else "standard", "", True, started_at, time.time())
            if queue_item_id:
                catalog.queue_set_status(CATALOG_DB, queue_item_id, "completed", finished_at=time.time())
        else:
            reason, retryable = _classify_download_error(error_text)
            with _lock:
                _download_details.setdefault(video_id, {}).update(
                    {"reason": reason, "error": error_text, "retryable": retryable}
                )
            catalog.record_attempt(CATALOG_DB, video_id, "failed", reason, ("[cookies] " if cookies_browser else "") + error_text,
                                   retryable, started_at, time.time())
            if queue_item_id:
                catalog.queue_set_status(CATALOG_DB, queue_item_id, "failed", error=error_text, finished_at=time.time())


def _cleanup_old_playlist_results() -> None:
    """Remove playlist download results older than TTL. Must be called with _lock held."""
    now = time.time()
    expired = [pid for pid, ts in _playlist_result_timestamps.items() if now - ts > _RESULT_TTL]
    for pid in expired:
        _playlist_download_results.pop(pid, None)
        _playlist_result_timestamps.pop(pid, None)


def _cleanup_old_playlist_history() -> None:
    """Remove playlist download page history older than TTL. Must be called with _lock held."""
    now = time.time()
    expired = [
        pid for pid, ts in _playlist_history_timestamps.items() if now - ts > _RESULT_TTL
    ]
    for pid in expired:
        _playlist_history.pop(pid, None)
        _playlist_history_timestamps.pop(pid, None)


def _prepare_playlist_membership(playlist_id: str, cookies_browser: str | None = None) -> Optional[list[dict]]:
    """Read playlist metadata first so skipped videos still become members."""
    try:
        url = getvidyt.to_youtube_url(playlist_id)
        opts = getvidyt.build_ydl_opts(
            merged_dir=MERGED_DIR, archive=CHECK_FILE, cookies_browser=cookies_browser,
            extra_opts={"skip_download": True, "extract_flat": "in_playlist", "quiet": True,
                        "no_warnings": True, "ignoreerrors": True},
        )
        with getvidyt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        entries = info.get("entries") or []
        members = []
        _ensure_cache()
        with _lock:
            archived = set(_cached_ids)
        for index, entry in enumerate(entries, 1):
            if not entry:
                continue
            video_id = str(entry.get("id") or "").strip()
            if not YOUTUBE_ID_RE.match(video_id):
                continue
            members.append({
                "video_id": video_id,
                "playlist_index": entry.get("playlist_index") or index,
                "title": entry.get("title") or video_id,
                "video_url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
            })
        catalog.record_playlist_membership(
            CATALOG_DB, playlist_id,
            str(info.get("title") or info.get("playlist_title") or playlist_id),
            str(info.get("webpage_url") or f"https://www.youtube.com/playlist?list={playlist_id}"),
            members,
        )
        with _lock:
            detail = _download_details.setdefault(playlist_id, {"logs": []})
            detail["playlist_items"] = {
                item["video_id"]: {**item, "status": "completed" if item["video_id"] in archived else "queued"}
                for item in members
            }
            detail["playlist_total"] = len(members)
            detail["playlist_index"] = sum(item["video_id"] in archived for item in members)
        return members
    except Exception as exc:
        app.logger.warning("Playlist preflight failed for %s; continuing download: %s", playlist_id, exc)
        return None


def _playlist_download_worker(playlist_id: str, queue_item_id: int | None = None) -> None:
    """Download all videos from a YouTube playlist via getvidyt."""
    members = _prepare_playlist_membership(playlist_id)
    try:
        if members is not None:
            # Download each child independently. This keeps a strict success
            # from being downloaded again when a different playlist child
            # needs the fallback format.
            rc = 0
            for member in members:
                child_rc = getvidyt.download_youtube(
                    member["video_id"],
                    audio_meta=True,
                    extra_opts={"progress_hooks": [_progress_hook(playlist_id)]},
                )
                if child_rc:
                    rc = child_rc
        else:
            # If preflight cannot read the playlist, do not retry the entire
            # playlist into the fallback tree; that can duplicate successes.
            rc = getvidyt.download_youtube(
                playlist_id,
                audio_meta=True,
                best_fallback=False,
                extra_opts={"progress_hooks": [_progress_hook(playlist_id)]},
            )
        # Force cache refresh so status can report archive contents
        global _cached_mtime
        with _lock:
            _cached_mtime = None
        _ensure_cache()
        success = rc == 0
    except Exception as e:
        app.logger.exception("Playlist download failed for %s: %s", playlist_id, e)
        success = False
    finally:
        with _lock:
            _active_playlist_downloads.discard(playlist_id)
            result = "completed" if success else "failed"
            finished_at = time.time()
            _playlist_download_results[playlist_id] = result
            _playlist_result_timestamps[playlist_id] = finished_at
            _playlist_history[playlist_id] = result
            _playlist_history_timestamps[playlist_id] = finished_at
            if queue_item_id:
                catalog.queue_set_status(CATALOG_DB, queue_item_id, result, finished_at=finished_at)
            _cleanup_old_playlist_results()
            _cleanup_old_playlist_history()
            _log_queue_state_locked()


def _start_queue_item(item: dict) -> bool:
    """Start one persisted queue item if its in-memory concurrency slot is free."""
    target, kind, item_id = item["target"], item["kind"], int(item["id"])
    with _lock:
        active = _active_downloads if kind == "video" else _active_playlist_downloads
        limit = MAX_CONCURRENT_DOWNLOADS if kind == "video" else MAX_CONCURRENT_PLAYLIST_DOWNLOADS
        if target in active or len(active) >= limit:
            return False
        active.add(target)
        started = time.time()
        (_download_started_at if kind == "video" else _playlist_started_at)[target] = started
        _download_details[target] = {"phase": "queued", "percent": 0, "filename": "", "files": {}, "logs": []}
        _log_queue_state_locked()
    catalog.queue_set_status(CATALOG_DB, item_id, "running", started_at=started)
    worker = _download_worker if kind == "video" else _playlist_download_worker
    args = (target, item.get("cookies_browser"), item_id) if kind == "video" else (target, item_id)
    threading.Thread(target=worker, args=args, daemon=True).start()
    return True


def _queue_scheduler() -> None:
    while True:
        try:
            now = time.time()
            for item in catalog.queue_items(CATALOG_DB, 100):
                if item["status"] != "pending":
                    continue
                if item["scheduled_at"] and item["scheduled_at"] > now:
                    continue
                _start_queue_item(item)
        except Exception:
            app.logger.exception("Download queue scheduler failed")
        _queue_wakeup.wait(2)
        _queue_wakeup.clear()


threading.Thread(target=_queue_scheduler, name="download-queue", daemon=True).start()


def _enqueue_target(target: str, kind: str, scheduled_at: float | None = None,
                    cookies_browser: str | None = None, status: str = "pending") -> tuple[int | None, bool]:
    """Add a target to the persistent queue, returning (id, already_queued)."""
    try:
        item_id = catalog.queue_add(CATALOG_DB, target, kind, scheduled_at, cookies_browser, status)
        _queue_wakeup.set()
        return item_id, False
    except sqlite3.IntegrityError:
        for item in catalog.queue_items(CATALOG_DB, 2000):
            if item["target"] == target and item["kind"] == kind and item["status"] in {"pending", "paused"}:
                return int(item["id"]), True
        raise


def _classify_file(fname: str, url: str, files: dict) -> None:
    """Slot a media file URL into the correct key of a video's files dict."""
    if _SIDECAR_RE.search(fname):
        return  # skip .f140.m4a / .f<n>.webm sidecars
    ext = Path(fname).suffix.lower()
    if ext == ".mkv":
        files["video"] = url
    elif ext in (".mp4", ".webm") and "video" not in files:
        files["video"] = url
    elif ext == ".m4a" and "audio" not in files:
        files["audio"] = url
    elif ext == ".opus" and "audio" not in files:
        files["audio"] = url
    elif ext == ".png" and "thumbnail" not in files:
        files["thumbnail"] = url
    elif ext in (".jpg", ".jpeg", ".webp") and "thumbnail" not in files:
        files["thumbnail"] = url
    elif ext == ".vtt" and "subtitles" not in files:
        files["subtitles"] = url
    elif ext == ".srt" and "subtitles" not in files:
        files["subtitles"] = url
    elif ext == ".json":
        files["info_json"] = url
    elif ext == ".description":
        files["description"] = url


def _file_kind(path: Path) -> str:
    name = path.name
    if _SIDECAR_RE.search(name):
        return "sidecar"
    ext = path.suffix.lower()
    if ext in {".mkv", ".mp4", ".webm"}:
        return "video"
    if ext in {".m4a", ".opus", ".mp3"}:
        return "audio"
    if ext in {".png", ".jpg", ".jpeg", ".webp"}:
        return "thumbnail"
    if ext in {".vtt", ".srt"}:
        return "subtitle"
    if name.endswith(".formats.json"):
        return "formats"
    if ext == ".json":
        return "metadata"
    if ext == ".description":
        return "description"
    if name.startswith("."):
        return "history"
    return "file"


def _media_file_entry(path: Path, channel_id: str, video_id: str, media_prefix: str = "/media") -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "url": f"{media_prefix}/{quote(channel_id, safe='')}/{quote(video_id, safe='')}/{quote(path.name, safe='')}",
        "kind": _file_kind(path),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def _media_file_url(channel_id: str, video_id: str, filename: str) -> str:
    return f"/media/{quote(channel_id, safe='')}/{quote(video_id, safe='')}/{quote(filename, safe='')}"


def _read_history_file(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        ts, _, value = line.partition(" ")
        rows.append({"timestamp": ts, "value": value})
    return rows


def _safe_metadata_value(value):
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _load_info_json(video_dir: Path) -> tuple[Optional[dict], Optional[str]]:
    for path in sorted(video_dir.glob("*.info.json")):
        try:
            return json.loads(path.read_text(encoding="utf-8", errors="ignore")), None
        except Exception as e:
            return None, str(e)
    return None, None


def _scan_library() -> list[dict]:
    """Walk primary and legacy merged trees, preferring the primary copy."""
    videos = []
    seen: Set[str] = set()
    for root, media_prefix in ((MERGED_DIR, "/media"), (LEGACY_MERGED_DIR, "/media-legacy")):
      if not root.exists():
        continue
      for channel_dir in sorted(root.iterdir()):
        if not channel_dir.is_dir():
            continue
        channel_id = channel_dir.name
        for video_dir in sorted(channel_dir.iterdir()):
            if not video_dir.is_dir():
                continue
            info, _info_error = _load_info_json(video_dir)
            if info and info.get("_type") == "playlist":
                continue
            video_id = video_dir.name
            if video_id in seen:
                continue
            seen.add(video_id)
            files: dict = {}
            detail_files: list[dict] = []
            description = ""
            title: Optional[str] = None
            date: Optional[str] = None
            for f in sorted(video_dir.iterdir()):
                if not f.is_file():
                    continue
                m = _FNAME_META_RE.match(f.name)
                if m and title is None:
                    title = m.group("title")
                    date = m.group("date")
                url = f"{media_prefix}/{quote(channel_id, safe='')}/{quote(video_id, safe='')}/{quote(f.name, safe='')}"
                _classify_file(f.name, url, files)
                detail_files.append(_media_file_entry(f, channel_id, video_id, media_prefix))
                if f.suffix.lower() == ".description" and not description:
                    try:
                        description = f.read_text(encoding="utf-8", errors="replace")[:2_000_000]
                    except OSError:
                        pass
            videos.append(
                {
                    "video_id": video_id,
                    "channel_id": channel_id,
                    "title": title or video_id,
                    "date": date,
                    "files": files,
                    "details": {"files": detail_files, "metadata": {"description": description}},
                }
            )
    return videos


def _resolve_media_by_video_id(video_id: str) -> Optional[dict]:
    for video in _scan_library():
        if video.get("video_id") != video_id:
            continue
        files = video.get("files") or {}
        player_url = files.get("video") or files.get("audio")
        return {
            **video,
            "player_url": player_url,
            "player_kind": "video" if files.get("video") else "audio" if files.get("audio") else None,
        }
    return None


def _scan_tags() -> dict:
    videos = _scan_library()
    tags: dict[str, list[dict]] = {}
    for video in videos:
        video_dir = MERGED_DIR / video["channel_id"] / video["video_id"]
        info, _error = _load_info_json(video_dir)
        for tag in info.get("tags") or [] if info else []:
            tag = str(tag).strip()
            if not tag:
                continue
            tags.setdefault(tag, []).append(video)

    return {
        "tags": [
            {"tag": tag, "count": len(items)}
            for tag, items in sorted(tags.items(), key=lambda item: (-len(item[1]), item[0].lower()))
        ],
        "videos_by_tag": tags,
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/video/<string:video_id>")
def video_page(video_id: str):
    if not _normalize_id(video_id):
        abort(404)
    return render_template("video.html", video_id=video_id)


@app.get("/extension.zip")
def download_extension():
    """Download the browser extension as a ZIP for local installation."""
    if not EXTENSION_DIR.is_dir():
        abort(404)

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(EXTENSION_DIR.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            bundle.write(path, path.relative_to(EXTENSION_DIR).as_posix())
    archive.seek(0)
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name="dihi-extension.zip",
        max_age=0,
    )


@app.get("/tags")
def tags_page():
    return render_template("tags.html")


@app.get("/downloads")
def downloads_page():
    return render_template("downloads.html")


@app.get("/queue")
def queue_page():
    return render_template("queue.html")


@app.get("/downloaded")
def downloaded_page():
    return render_template("downloaded.html")


@app.get("/catalog")
def catalog_page():
    return render_template("catalog.html")


@app.get("/wordcloud")
def wordcloud_page():
    return render_template("wordcloud.html")


@app.get("/tagcloud")
def tagcloud_page():
    return render_template("tagcloud.html")


@app.get("/status")
def status_page():
    return render_template("status.html")


@app.get("/tools")
def tools_page():
    return render_template("tools.html")


@app.get("/api-docs")
def api_docs_page():
    endpoints = [
        ("GET", "/health", "Health and active download counts"),
        ("GET", "/api/youtube/<video_id>", "Check archive status"),
        ("POST", "/api/youtube/get/<video_id>", "Add or start a video download"),
        ("POST", "/api/youtube/retry/<video_id>", "Retry a failed video"),
        ("GET", "/api/youtube/status/<video_id>", "Video download progress"),
        ("POST", "/api/youtube/playlist/get/<playlist_id>", "Add or start a playlist download"),
        ("GET", "/api/youtube/playlist/status/<playlist_id>", "Playlist download progress"),
        ("GET", "/api/downloads/status", "Active and recent download status"),
        ("GET", "/api/queue", "Persistent queue items and default mode"),
        ("POST", "/api/queue", "Add a video or playlist to the queue"),
        ("POST", "/api/queue/<item_id>/start", "Start a paused queue item"),
        ("POST", "/api/queue/<item_id>/cancel", "Cancel a pending queue item"),
        ("GET/POST", "/api/settings", "Read or update queue defaults"),
        ("GET", "/api/media/library", "List local media"),
        ("GET", "/api/media/resolve/<video_id>", "Resolve playable local media"),
        ("GET", "/api/media/details/<channel_id>/<video_id>", "Return files and metadata"),
        ("GET", "/api/media/playlists", "List indexed playlists"),
        ("GET", "/api/media/playlists/<playlist_id>", "Return playlist members"),
        ("GET", "/api/media/playlists/<playlist_id>.m3u?mode=video|audio", "Export a VLC playlist"),
        ("GET", "/api/media/catalog", "Paginated catalog data"),
        ("GET", "/api/media/tags", "Tag counts and grouped videos"),
        ("GET", "/api/media/failures", "Latest unresolved failures"),
        ("GET", "/api/media/download-history", "Persistent download history"),
        ("GET", "/api/media/wordcloud", "Description word frequencies"),
        ("GET", "/api/system/status", "Disk, directory, and cookie diagnostics"),
    ]
    return render_template("api-docs.html", endpoints=endpoints)


@app.get("/sitemap")
def sitemap_page():
    groups = [
        ("Library", [("Media library", "/"), ("Playlists", "/playlists"), ("Video detail", "/video/dQw4w9WgXcQ"), ("Catalog", "/catalog")]),
        ("Downloads", [("Downloads status", "/downloads"), ("Persistent queue", "/queue"), ("Downloaded history", "/downloaded")]),
        ("Tools", [("Tools hub", "/tools"), ("API documentation", "/api-docs"), ("System status", "/status"), ("Tags", "/tags"), ("Word cloud", "/wordcloud"), ("Tag cloud", "/tagcloud")]),
        ("Integration", [("Health API", "/health"), ("Extension download", "/extension.zip")]),
    ]
    return render_template("sitemap.html", groups=groups)


@app.get("/playlists")
def playlists_page():
    return render_template("playlists.html")


@app.get("/api/media/wordcloud")
def api_media_wordcloud():
    """Return word frequencies from saved descriptions, optionally filtered."""
    tag = request.args.get("tag", "").strip().lower()
    playlist = request.args.get("playlist", "").strip().lower()
    stop = {
        "the", "and", "you", "that", "this", "with", "from", "your", "for", "are", "was",
        "auf", "und", "der", "die", "das", "to", "of", "a", "in", "on", "is", "it",
        "http", "https", "www", "com", "youtube", "provided", "music", "copyright",
        # Description credits, promotion, and production boilerplate.
        "video", "lyrics", "channel", "director", "producer", "production", "gaffer", "mua",
        "official", "support", "subscribe", "instagram", "spotify", "album", "management",
        "tickets", "written", "welcome", "song",
    }
    counts: dict[str, int] = {}
    try:
        with sqlite3.connect(CATALOG_DB) as db:
            rows = db.execute("SELECT metadata_json FROM videos").fetchall()
    except sqlite3.Error:
        return jsonify(words=[]), 200
    for (raw,) in rows:
        try: info = json.loads(raw or "{}")
        except ValueError: continue
        tags = {str(x).lower() for x in (info.get("tags") or [])}
        if tag and tag not in tags: continue
        if playlist and playlist not in str(info.get("playlist_title") or info.get("playlist") or "").lower(): continue
        text = re.sub(r"https?://\S+|www\.\S+", " ", str(info.get("description") or ""), flags=re.IGNORECASE)
        for word in re.findall(r"[\wÀ-ÿ']{3,}", text.lower(), re.UNICODE):
            if word not in stop and not word.isdigit(): counts[word] = counts.get(word, 0) + 1
    return jsonify(words=[{"word": w, "count": n} for w, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:300]])


@app.get("/api/media/wordcloud/videos")
def api_media_wordcloud_videos():
    """Return archived videos whose saved description contains a word."""
    word = request.args.get("word", "").strip().lower()
    if not re.fullmatch(r"[\wÀ-ÿ']{3,}", word, re.UNICODE):
        return jsonify(video_ids=[])
    matches = []
    try:
        with sqlite3.connect(CATALOG_DB) as db:
            rows = db.execute("SELECT video_id, metadata_json FROM videos").fetchall()
    except sqlite3.Error:
        return jsonify(video_ids=[])
    pattern = re.compile(rf"(?<![\wÀ-ÿ]){re.escape(word)}(?![\wÀ-ÿ])", re.IGNORECASE)
    for video_id, raw in rows:
        try:
            info = json.loads(raw or "{}")
        except ValueError:
            continue
        if pattern.search(str(info.get("description") or "")):
            matches.append(video_id)
    return jsonify(video_ids=matches)


@app.get("/api/media/catalog")
@limiter.limit("60 per minute")
def api_media_catalog():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    except ValueError:
        return jsonify(error="page and per_page must be integers"), 400
    sort = request.args.get("sort", "date-desc")
    order = "upload_date ASC" if sort == "date-asc" else "artist COLLATE NOCASE ASC, title COLLATE NOCASE ASC" if sort == "artist" else "title COLLATE NOCASE ASC" if sort == "title" else "upload_date DESC"
    try:
        with sqlite3.connect(CATALOG_DB) as db:
            total = db.execute("SELECT COUNT(DISTINCT video_id) FROM videos").fetchone()[0]
            # A video may exist in both the primary and fallback trees. Show
            # one catalog row, preferring the complete primary copy.
            rows = db.execute(f"""SELECT video_id, source_root, channel_id, title, artist, album, uploader, upload_date, duration, files_json, formats_json,
                (SELECT CASE WHEN a.status='failed' THEN a.reason ELSE '' END
                 FROM download_attempts a WHERE a.video_id=catalog_rows.video_id
                 ORDER BY a.finished_at DESC LIMIT 1) AS failure_reason
                FROM (SELECT v.*, ROW_NUMBER() OVER (PARTITION BY video_id ORDER BY CASE source_root WHEN 'merged' THEN 0 WHEN 'legacy' THEN 1 ELSE 2 END) AS rn FROM videos v) AS catalog_rows
                WHERE rn = 1 ORDER BY {order} LIMIT ? OFFSET ?""", (per_page, (page - 1) * per_page)).fetchall()
    except sqlite3.Error:
        return jsonify(error="catalog is still being built"), 503
    items = []
    for row in rows:
        files = json.loads(row[9] or "{}")
        items.append({"video_id": row[0], "source_root": row[1], "channel_id": row[2], "title": row[3], "artist": row[4], "album": row[5], "uploader": row[6], "upload_date": row[7], "duration": row[8], "files": files, "formats": json.loads(row[10] or "[]"), "failure_reason": row[11] or ""})
    return jsonify(items=items, page=page, per_page=per_page, total=total, pages=(total + per_page - 1) // per_page)


@app.get("/api/media/library")
def api_media_library():
    return jsonify(videos=_scan_library())


@app.get("/api/media/playlists")
def api_media_playlists():
    return jsonify(playlists=catalog.playlists(CATALOG_DB))


@app.get("/api/media/playlists/<string:playlist_id>")
def api_media_playlist(playlist_id: str):
    if not PLAYLIST_ID_RE.match(playlist_id):
        return jsonify(error="invalid playlist id"), 400
    playlist_rows = catalog.playlists(CATALOG_DB)
    playlist = next((item for item in playlist_rows if item["playlist_id"] == playlist_id), None)
    if not playlist:
        return jsonify(error="playlist not found"), 404
    videos_by_id = {item["video_id"]: item for item in _scan_library()}
    members = []
    for member in catalog.playlist_video_ids(CATALOG_DB, playlist_id):
        video = videos_by_id.get(member["video_id"])
        members.append({**member, "video": video})
    return jsonify(playlist=playlist, videos=members)


@app.get("/api/media/playlists/<string:playlist_id>.m3u")
def api_media_playlist_m3u(playlist_id: str):
    if not PLAYLIST_ID_RE.match(playlist_id):
        return jsonify(error="invalid playlist id"), 400
    mode = request.args.get("mode", "video").strip().lower()
    if mode not in {"video", "audio"}:
        return jsonify(error="mode must be video or audio"), 400
    members = catalog.playlist_video_ids(CATALOG_DB, playlist_id)
    videos_by_id = {item["video_id"]: item for item in _scan_library()}
    lines = ["#EXTM3U"]
    for member in members:
        video = videos_by_id.get(member["video_id"]) or {}
        files = video.get("files") or {}
        media_url = files.get(mode)
        if not media_url:
            continue
        lines.extend([
            f"#EXTINF:-1,{member.get('title') or video.get('title') or member['video_id']}",
            request.host_url.rstrip("/") + media_url,
        ])
    return Response("\n".join(lines) + "\n", mimetype="audio/x-mpegurl",
                    headers={"Content-Disposition": f'attachment; filename="{playlist_id}-{mode}.m3u"'})


@app.get("/api/media/tags")
@limiter.limit("30 per minute")
def api_media_tags():
    return jsonify(_scan_tags())


@app.get("/api/media/resolve/<string:video_id>")
@limiter.limit("60 per minute")
def api_media_resolve(video_id: str):
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(error="invalid video id"), 400

    video = _resolve_media_by_video_id(vid)
    if not video or not video.get("player_url"):
        return jsonify(result=False, video_id=vid, reason="metadata exists but no playable media file is present"), 404

    return jsonify(result=True, video=video)


@app.get("/api/downloads/status")
@limiter.limit("60 per minute")
def api_downloads_status():
    return jsonify(_download_status_snapshot())


@app.get("/api/queue")
def api_queue_list():
    return jsonify(items=catalog.queue_items(CATALOG_DB),
                   default_mode=catalog.setting(CATALOG_DB, "default_download_mode", "immediate"))


@app.post("/api/queue")
def api_queue_add():
    payload = request.get_json(silent=True) or {}
    raw = str(payload.get("target") or "").strip()
    # Accept IDs and the same YouTube URL forms as the library download bar.
    video_id = _normalize_id(raw)
    kind = "video" if video_id else "playlist"
    target = video_id or _normalize_playlist_id(raw)
    if not target:
        match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", raw)
        target = _normalize_id(match.group(1)) if match else None
        kind = "video"
        if not target:
            match = re.search(r"[?&]list=([A-Za-z0-9_-]{2,128})", raw)
            target = _normalize_playlist_id(match.group(1)) if match else None
            kind = "playlist"
    if not target:
        return jsonify(ok=False, error="enter a YouTube video ID, playlist ID, or URL"), 400
    scheduled_at = payload.get("scheduled_at")
    try:
        scheduled_at = float(scheduled_at) if scheduled_at not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify(ok=False, error="scheduled_at must be a timestamp"), 400
    start_now = payload.get("start_now")
    if start_now is None:
        start_now = catalog.setting(CATALOG_DB, "default_download_mode", "immediate") == "immediate"
    if scheduled_at is not None:
        start_now = False
    queue_status = "pending" if start_now or scheduled_at is not None else "paused"
    item_id, already = _enqueue_target(target, kind, scheduled_at, status=queue_status)
    if start_now and not scheduled_at:
        _queue_wakeup.set()
    return jsonify(ok=True, id=item_id, target=target, kind=kind, already_queued=already, started=bool(start_now and not already))


@app.post("/api/queue/<int:item_id>/start")
def api_queue_start(item_id: int):
    for item in catalog.queue_items(CATALOG_DB, 2000):
        if int(item["id"]) == item_id and item["status"] in {"pending", "paused"}:
            catalog.queue_set_status(CATALOG_DB, item_id, "pending", scheduled_at=time.time())
            _queue_wakeup.set()
            return jsonify(ok=True, id=item_id)
    return jsonify(ok=False, error="pending queue item not found"), 404


@app.post("/api/queue/<int:item_id>/cancel")
def api_queue_cancel(item_id: int):
    for item in catalog.queue_items(CATALOG_DB, 2000):
        if int(item["id"]) == item_id and item["status"] == "pending":
            catalog.queue_set_status(CATALOG_DB, item_id, "cancelled", finished_at=time.time())
            return jsonify(ok=True, id=item_id)
    return jsonify(ok=False, error="pending queue item not found"), 404


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("default_download_mode") or "").strip().lower()
        if mode not in {"immediate", "queue"}:
            return jsonify(ok=False, error="default_download_mode must be immediate or queue"), 400
        catalog.set_setting(CATALOG_DB, "default_download_mode", mode)
    return jsonify(default_download_mode=catalog.setting(CATALOG_DB, "default_download_mode", "immediate"))


@app.get("/api/media/details/<string:channel_id>/<string:video_id>")
def api_media_details(channel_id: str, video_id: str):
    if not PLAYLIST_ID_RE.match(channel_id) or not YOUTUBE_ID_RE.match(video_id):
        return jsonify(error="invalid id"), 400

    video_dir = (MERGED_DIR / channel_id / video_id).resolve()
    media_root = MERGED_DIR
    if not video_dir.is_dir():
        video_dir = (LEGACY_MERGED_DIR / channel_id / video_id).resolve()
        media_root = LEGACY_MERGED_DIR
    if not video_dir.is_relative_to(media_root):
        abort(403)
    if not video_dir.is_dir():
        abort(404)

    files = [
        _media_file_entry(path, channel_id, video_id)
        for path in sorted(video_dir.iterdir())
        if path.is_file()
    ]
    info_json, info_error = _load_info_json(video_dir)
    if info_json:
        info_json = {k: _safe_metadata_value(v) for k, v in info_json.items()}

    channel_dir = MERGED_DIR / channel_id
    description = ""
    description_files = sorted(video_dir.glob("*.description"))
    if description_files:
        try:
            description = description_files[0].read_text(encoding="utf-8", errors="replace")[:2_000_000]
        except OSError:
            description = ""
    metadata = {
        "channel": {
            "channel_name": _read_history_file(channel_dir / ".channel_name"),
            "uploader_id": _read_history_file(channel_dir / ".uploader_id"),
            "uploader_name": _read_history_file(channel_dir / ".uploader_name"),
        },
        "video": {
            "title_name": _read_history_file(video_dir / ".title_name"),
            "upload_date": _read_history_file(video_dir / ".upload_date"),
        },
        "info_json": info_json,
        "info_json_error": info_error,
        "description": description,
    }

    return jsonify(channel_id=channel_id, video_id=video_id, files=files, metadata=metadata)


@app.get("/media/<path:filepath>")
def serve_media(filepath: str):
    try:
        full_path = (MERGED_DIR / filepath).resolve()
    except Exception:
        abort(400)
    if not full_path.is_relative_to(MERGED_DIR):
        abort(403)
    if not full_path.is_file():
        abort(404)
    response = send_file(full_path, conditional=True)
    response.headers.setdefault("Accept-Ranges", "bytes")
    response.headers.setdefault("Cache-Control", "public, max-age=86400")
    return response


@app.get("/media-fallback/<path:filepath>")
def serve_fallback_media(filepath: str):
    try:
        full_path = (FALLBACK_DIR / filepath).resolve()
    except Exception:
        abort(400)
    if not full_path.is_relative_to(FALLBACK_DIR) or not full_path.is_file():
        abort(404)
    return send_file(full_path, conditional=True)


@app.get("/media-legacy/<path:filepath>")
def serve_legacy_media(filepath: str):
    try:
        full_path = (LEGACY_MERGED_DIR / filepath).resolve()
    except Exception:
        abort(400)
    if not full_path.is_relative_to(LEGACY_MERGED_DIR) or not full_path.is_file():
        abort(404)
    return send_file(full_path, conditional=True)


@app.get("/api/youtube/<string:video_id>")
@limiter.limit("60 per minute")
def api_youtube_check(video_id: str):
    """
    GET /api/youtube/<id>
    Returns exactly: {"result": true|false}
    """
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(error="invalid video id"), 400

    _ensure_cache()
    with _lock:
        found = vid in _cached_ids

    return jsonify(result=bool(found))


@app.post("/api/youtube/get/<string:video_id>")
@limiter.limit("10 per minute")
def api_youtube_get(video_id: str):
    """
    POST /api/youtube/get/<id>

    Triggers:
      import getvidyt
      getvidyt.download_youtube("<id>", audio_meta=True)

    Does NOT modify archive.txt.
    """
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(ok=False, error="invalid video id"), 400

    queue_status = "pending" if catalog.setting(CATALOG_DB, "default_download_mode", "immediate") == "immediate" else "paused"
    item_id, already_queued = _enqueue_target(vid, "video", status=queue_status)

    return jsonify(
        ok=True,
        id=vid,
        queue_id=item_id,
        started=not already_queued,
        already_running=already_queued,
    )


@app.post("/api/youtube/retry/<string:video_id>")
@limiter.limit("10 per minute")
def api_youtube_retry(video_id: str):
    """Explicitly retry a failed or partial download, resuming local parts."""
    authenticated = request.args.get("authenticated") == "1"
    browser = request.args.get("browser", "").strip() or None
    if authenticated:
        # The normal cookiefile (data/cookies.txt) is always used by getvidyt;
        # this optional value adds cookies-from-browser for local installs.
        vid = _normalize_id(video_id)
        if not vid:
            return jsonify(error="invalid video id"), 400
        item_id, already_queued = _enqueue_target(vid, "video", cookies_browser=browser)
        return jsonify(ok=True, id=vid, queue_id=item_id, started=not already_queued, authenticated=True)
    return api_youtube_get(video_id)


@app.get("/api/media/failures")
def api_media_failures():
    with sqlite3.connect(CATALOG_DB) as db:
        db.executescript(catalog.SCHEMA)
        rows = db.execute("""SELECT latest.video_id,latest.status,latest.reason,latest.raw_error,latest.retryable,latest.started_at,latest.finished_at
          FROM (SELECT a.*, ROW_NUMBER() OVER (PARTITION BY a.video_id ORDER BY a.finished_at DESC) rn FROM download_attempts a)
          AS latest
          WHERE latest.rn=1 AND latest.status='failed' AND NOT EXISTS
          (SELECT 1 FROM archive_entries e WHERE e.video_id=latest.video_id AND e.status='complete')
          ORDER BY latest.finished_at DESC""").fetchall()
    return jsonify(failures=[{"video_id":r[0],"status":r[1],"reason":r[2],"error":r[3],"retryable":bool(r[4]),"started_at":r[5],"finished_at":r[6],"age_restricted":r[2]=="age_restricted","authenticated":str(r[3] or '').startswith('[cookies]')} for r in rows])


@app.get("/api/media/download-history")
def api_media_download_history():
    """Return persistent completed and failed download attempts."""
    try:
        rows = catalog.download_attempts(CATALOG_DB, request.args.get("limit", 500))
    except (TypeError, ValueError):
        return jsonify(error="limit must be a number"), 400
    return jsonify(attempts=rows)


@app.get("/api/system/status")
def api_system_status():
    """Return safe operational diagnostics without exposing cookie contents."""
    usage = shutil.disk_usage(Path.cwd())
    # Docker mounts the host export at /app/cookies.txt; local runs commonly
    # keep it at data/cookies.txt. Inspect whichever active path exists.
    cookie_candidates = [Path("./cookies.txt"), Path("./data/cookies.txt")]
    cookie = next((candidate for candidate in cookie_candidates if candidate.is_file()), cookie_candidates[0])
    def directory_size(path: Path) -> int:
        total = 0
        if not path.is_dir():
            return total
        try:
            for item in path.rglob("*"):
                if item.is_file():
                    try: total += item.stat().st_size
                    except OSError: pass
        except OSError: pass
        return total
    result = {"disk": {"free_bytes": usage.free, "total_bytes": usage.total, "free_percent": round(usage.free * 100 / usage.total, 1)}, "directories": {str(path): directory_size(path) for path in (MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR, Path("./audio").resolve())}, "cookies": {"present": cookie.is_file(), "netscape_format": False, "youtube_domains": [], "count": 0, "expired": 0}}
    if cookie.is_file():
        try:
            lines = cookie.read_text(encoding="utf-8", errors="ignore").splitlines()
            result["cookies"]["netscape_format"] = any(line.startswith("# Netscape HTTP Cookie File") for line in lines)
            now = time.time()
            for line in lines:
                if not line or line.startswith("#") or len(line.split("\t")) < 7: continue
                fields = line.split("\t"); domain = fields[0].lstrip(".").lower(); result["cookies"]["count"] += 1
                if "youtube.com" in domain or "google.com" in domain: result["cookies"]["youtube_domains"].append(domain)
                try:
                    if float(fields[4]) and float(fields[4]) < now: result["cookies"]["expired"] += 1
                except ValueError: pass
            result["cookies"]["youtube_domains"] = sorted(set(result["cookies"]["youtube_domains"]))
        except OSError: pass
    return jsonify(result)


@app.get("/api/youtube/status/<string:video_id>")
@limiter.limit("60 per minute")
def api_youtube_status(video_id: str):
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(error="invalid video id"), 400

    with _lock:
        downloading = vid in _active_downloads
        result = _download_results.get(vid)
        detail = dict(_download_details.get(vid, {}))
        # Clear result after reading (one-time consumption)
        if result and not downloading:
            _download_results.pop(vid, None)
            _result_timestamps.pop(vid, None)

    # Also check archive status for complete picture
    _ensure_cache()
    with _lock:
        in_archive = vid in _cached_ids

    return jsonify(
        downloading=bool(downloading),
        id=vid,
        result=result,  # "completed", "failed", or None
        in_archive=in_archive,
        **detail,
    )


@app.post("/api/youtube/playlist/get/<string:playlist_id>")
@limiter.limit("5 per minute")
def api_youtube_playlist_get(playlist_id: str):
    """
    POST /api/youtube/playlist/get/<playlist_id>

    Triggers download of all videos in a YouTube playlist.
    Uses getvidyt.download_youtube() which natively handles playlists.
    """
    pid = _normalize_playlist_id(playlist_id)
    if not pid:
        return jsonify(ok=False, error="invalid playlist id"), 400

    queue_status = "pending" if catalog.setting(CATALOG_DB, "default_download_mode", "immediate") == "immediate" else "paused"
    item_id, already_queued = _enqueue_target(pid, "playlist", status=queue_status)

    return jsonify(
        ok=True,
        id=pid,
        queue_id=item_id,
        started=not already_queued,
        already_running=already_queued,
    )


@app.get("/api/youtube/playlist/status/<string:playlist_id>")
@limiter.limit("60 per minute")
def api_youtube_playlist_status(playlist_id: str):
    """
    GET /api/youtube/playlist/status/<playlist_id>

    Poll playlist download progress.
    """
    pid = _normalize_playlist_id(playlist_id)
    if not pid:
        return jsonify(error="invalid playlist id"), 400

    with _lock:
        downloading = pid in _active_playlist_downloads
        result = _playlist_download_results.get(pid)
        detail = dict(_download_details.get(pid, {}))
        if result and not downloading:
            _playlist_download_results.pop(pid, None)
            _playlist_result_timestamps.pop(pid, None)

    return jsonify(
        downloading=bool(downloading),
        id=pid,
        result=result,  # "completed", "failed", or None
        **detail,
    )


@app.get("/health")
@limiter.limit("30 per minute")
def health():
    return jsonify(
        ok=True,
        archive_exists=CHECK_FILE.exists(),
        active_downloads=len(_active_downloads),
        max_concurrent=MAX_CONCURRENT_DOWNLOADS,
        active_playlist_downloads=len(_active_playlist_downloads),
        max_concurrent_playlists=MAX_CONCURRENT_PLAYLIST_DOWNLOADS,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # threaded=True allows concurrent requests while a download thread runs
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
