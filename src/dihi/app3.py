#!/usr/bin/env python3
from __future__ import annotations

import io
import mimetypes
import json
import os
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional, Set

from flask import Flask, abort, jsonify, render_template, request, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("text/vtt", ".vtt")
mimetypes.add_type("audio/opus", ".opus")

import getvidyt  # must be importable in this environment

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
            detail.update({"phase": phase, "filename": filename, "percent": percent})
            logs = detail.setdefault("logs", [])
            if not logs or logs[-1] != phase:
                logs.append(phase)
                del logs[:-40]
    return hook


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


def _download_worker(video_id: str) -> None:
    """
    Actually runs:
      getvidyt.download_youtube(video_id, audio_meta=True)
    Tracks completion status for proper UI feedback.
    """
    success = False
    try:
        getvidyt.download_youtube(
            video_id,
            audio_meta=True,
            extra_opts={"progress_hooks": [_progress_hook(video_id)]},
        )
        # Give filesystem time to sync archive.txt
        time.sleep(0.5)
        # Force cache refresh and check if video is now in archive
        global _cached_mtime
        with _lock:
            _cached_mtime = None  # Force refresh
        _ensure_cache()
        with _lock:
            success = video_id in _cached_ids
    except Exception as e:
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


def _playlist_download_worker(playlist_id: str) -> None:
    """Download all videos from a YouTube playlist via getvidyt."""
    try:
        rc = getvidyt.download_youtube(
            playlist_id,
            audio_meta=True,
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
            _cleanup_old_playlist_results()
            _cleanup_old_playlist_history()
            _log_queue_state_locked()


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


def _media_file_entry(path: Path, channel_id: str, video_id: str) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "url": f"/media/{channel_id}/{video_id}/{path.name}",
        "kind": _file_kind(path),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def _media_file_url(channel_id: str, video_id: str, filename: str) -> str:
    return f"/media/{channel_id}/{video_id}/{filename}"


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
    """Walk merged/<channel>/<video_id>/ and return a list of video dicts."""
    if not MERGED_DIR.exists():
        return []
    videos = []
    for channel_dir in sorted(MERGED_DIR.iterdir()):
        if not channel_dir.is_dir():
            continue
        channel_id = channel_dir.name
        for video_dir in sorted(channel_dir.iterdir()):
            if not video_dir.is_dir():
                continue
            video_id = video_dir.name
            files: dict = {}
            title: Optional[str] = None
            date: Optional[str] = None
            for f in sorted(video_dir.iterdir()):
                if not f.is_file():
                    continue
                m = _FNAME_META_RE.match(f.name)
                if m and title is None:
                    title = m.group("title")
                    date = m.group("date")
                url = _media_file_url(channel_id, video_id, f.name)
                _classify_file(f.name, url, files)
            videos.append(
                {
                    "video_id": video_id,
                    "channel_id": channel_id,
                    "title": title or video_id,
                    "date": date,
                    "files": files,
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


@app.get("/api/media/library")
@limiter.limit("30 per minute")
def api_media_library():
    return jsonify(videos=_scan_library())


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
    if not video:
        return jsonify(result=False, video_id=vid), 404

    return jsonify(result=True, video=video)


@app.get("/api/downloads/status")
@limiter.limit("60 per minute")
def api_downloads_status():
    return jsonify(_download_status_snapshot())


@app.get("/api/media/details/<string:channel_id>/<string:video_id>")
@limiter.limit("60 per minute")
def api_media_details(channel_id: str, video_id: str):
    if not PLAYLIST_ID_RE.match(channel_id) or not YOUTUBE_ID_RE.match(video_id):
        return jsonify(error="invalid id"), 400

    video_dir = (MERGED_DIR / channel_id / video_id).resolve()
    if not video_dir.is_relative_to(MERGED_DIR):
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

    with _lock:
        already_running = vid in _active_downloads
        if not already_running:
            # Check max concurrent downloads limit
            if len(_active_downloads) >= MAX_CONCURRENT_DOWNLOADS:
                return jsonify(ok=False, error="too many concurrent downloads"), 429
            _active_downloads.add(vid)
            _download_started_at[vid] = time.time()
            _download_details[vid] = {"phase": "queued", "percent": 0, "filename": "", "logs": []}
            _log_queue_state_locked()
            threading.Thread(target=_download_worker, args=(vid,), daemon=True).start()

    return jsonify(
        ok=True,
        id=vid,
        started=(not already_running),
        already_running=already_running,
    )


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

    with _lock:
        already_running = pid in _active_playlist_downloads
        if not already_running:
            if len(_active_playlist_downloads) >= MAX_CONCURRENT_PLAYLIST_DOWNLOADS:
                return jsonify(ok=False, error="too many concurrent playlist downloads"), 429
            _active_playlist_downloads.add(pid)
            _playlist_started_at[pid] = time.time()
            _download_details[pid] = {"phase": "queued", "percent": 0, "filename": "", "logs": []}
            _log_queue_state_locked()
            threading.Thread(
                target=_playlist_download_worker, args=(pid,), daemon=True
            ).start()

    return jsonify(
        ok=True,
        id=pid,
        started=(not already_running),
        already_running=already_running,
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
