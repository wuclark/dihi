#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Optional, Set

from flask import Flask, jsonify
from flask_cors import CORS

import getvidyt  # must be importable in this environment

app = Flask(__name__)

# CORS: Restrict to trusted origins
CORS(app, origins=[
    "https://www.youtube.com",
    "https://youtube.com",
    "chrome-extension://*",
    "moz-extension://*",
])

# Validate YouTube video IDs (11 chars: alphanumeric, underscore, dash)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Max concurrent downloads to prevent resource exhaustion
MAX_CONCURRENT_DOWNLOADS = 5

# Archive lines look like: "youtube <id>"
CHECK_FILE = Path("./archive.txt").expanduser().resolve()

_lock = threading.Lock()
_cached_mtime: Optional[float] = None
_cached_ids: Set[str] = set()

_active_downloads: Set[str] = set()  # prevent spamming duplicate downloads


def _normalize_id(raw: str) -> Optional[str]:
    """Normalize and validate YouTube video ID."""
    vid = (raw or "").strip()
    if not vid or not YOUTUBE_ID_RE.match(vid):
        return None
    return vid


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


def _download_worker(video_id: str) -> None:
    """
    Actually runs:
      getvidyt.download_youtube(video_id)
    """
    try:
        getvidyt.download_youtube(video_id)
    except Exception as e:
        app.logger.exception("Download failed for %s: %s", video_id, e)
    finally:
        with _lock:
            _active_downloads.discard(video_id)


@app.get("/api/youtube/<string:video_id>")
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
def api_youtube_get(video_id: str):
    """
    POST /api/youtube/get/<id>

    Triggers:
      import getvidyt
      getvidyt.download_youtube("<id>")

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
            threading.Thread(target=_download_worker, args=(vid,), daemon=True).start()

    return jsonify(
        ok=True,
        id=vid,
        started=(not already_running),
        already_running=already_running,
    )


@app.get("/api/youtube/status/<string:video_id>")
def api_youtube_status(video_id: str):
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(error="invalid video id"), 400

    with _lock:
        downloading = vid in _active_downloads

    return jsonify(downloading=bool(downloading), id=vid)


@app.get("/health")
def health():
    return jsonify(
        ok=True,
        archive_exists=CHECK_FILE.exists(),
        active_downloads=len(_active_downloads),
        max_concurrent=MAX_CONCURRENT_DOWNLOADS,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # threaded=True allows concurrent requests while a download thread runs
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

