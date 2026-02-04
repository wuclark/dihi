#!/usr/bin/env python3
"""
Flask API: YouTube ID checker + "get" action

Archive format:
  Each line looks like:
    youtube <id>

Endpoints:
  - GET  /api/youtube/<id>          -> {"result": true|false}  (checks archive.txt)
  - POST /api/youtube/get/<id>      -> triggers getvidyt.download_youtube(id)
                                      (does NOT modify archive.txt)
  - GET  /api/youtube/status/<id>   -> {"downloading": true|false}

Notes:
  - Download runs in a background thread.
  - Active downloads tracked in-memory (_active_downloads).
  - Archive is cached by file mtime for fast GET checks.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional, Set

from flask import Flask, jsonify
from flask_cors import CORS

import getvidyt  # make sure this is installed/importable

app = Flask(__name__)
CORS(app)  # Enable CORS for browser extension

CHECK_FILE = Path("./archive.txt").expanduser().resolve()

_lock = threading.Lock()
_cached_mtime: Optional[float] = None
_cached_ids: Set[str] = set()          # IDs from lines: "youtube <id>"
_active_downloads: Set[str] = set()    # IDs currently downloading


def _normalize_id(raw: str) -> str:
    return (raw or "").strip()


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
    try:
        getvidyt.download_youtube(video_id)
    except Exception as e:
        app.logger.exception("Download failed for %s: %s", video_id, e)
    finally:
        with _lock:
            _active_downloads.discard(video_id)


@app.get("/api/youtube/<string:video_id>")
def api_youtube_check(video_id: str):
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(error="empty id"), 400

    _ensure_cache()
    with _lock:
        found = vid in _cached_ids

    return jsonify(result=bool(found))


@app.post("/api/youtube/get/<string:video_id>")
def api_youtube_get(video_id: str):
    """
    Triggers:
      getvidyt.download_youtube("<id>")
    Does NOT write to archive.txt.
    """
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(ok=False, error="empty id"), 400

    with _lock:
        already_running = vid in _active_downloads
        if not already_running:
            _active_downloads.add(vid)
            threading.Thread(target=_download_worker, args=(vid,), daemon=True).start()

    return jsonify(ok=True, id=vid, started=(not already_running), already_running=already_running)


@app.get("/api/youtube/status/<string:video_id>")
def api_youtube_status(video_id: str):
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(error="empty id"), 400

    with _lock:
        downloading = vid in _active_downloads

    return jsonify(downloading=bool(downloading), id=vid)


@app.get("/health")
def health():
    return jsonify(
        ok=True,
        file=str(CHECK_FILE),
        exists=CHECK_FILE.exists(),
        cached_ids=len(_cached_ids),
        active_downloads_count=len(_active_downloads),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
