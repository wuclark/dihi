#!/usr/bin/env python3
from __future__ import annotations

import io
import hashlib
import logging
import mimetypes
import json
import os
import re
import sqlite3
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional, Set
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, make_response, render_template, request, send_file
from flask_cors import CORS

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("text/vtt", ".vtt")
mimetypes.add_type("audio/opus", ".opus")

import getvidyt  # must be importable in this environment
import catalog
from library_helpers import clean_display_title, human_date

app = Flask(__name__)
CORS(app)  # Allow all origins
app.logger.setLevel(logging.INFO)

# Validate YouTube video IDs (11 chars: alphanumeric, underscore, dash)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Validate YouTube playlist IDs (alphanumeric, underscore, dash, 2-128 chars)
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,128}$")

# Max concurrent downloads to prevent resource exhaustion
MAX_CONCURRENT_DOWNLOADS = 5
MAX_CONCURRENT_PLAYLIST_DOWNLOADS = 2


def _queue_limit(kind: str) -> int:
    key, default, maximum = ("max_concurrent_playlists", MAX_CONCURRENT_PLAYLIST_DOWNLOADS, 5) if kind == "playlist" else ("max_concurrent_downloads", MAX_CONCURRENT_DOWNLOADS, 10)
    try:
        return max(1, min(maximum, int(catalog.setting(CATALOG_DB, key, str(default)))))
    except (ValueError, TypeError, sqlite3.Error):
        return default

# Archive lines look like: "youtube <id>"
DATA_ROOT = Path(os.environ.get("DIHI_DATA_DIR", "./data")).expanduser().resolve()
CHECK_FILE = DATA_ROOT / "archive.txt"
MERGED_DIR = DATA_ROOT / "media-strict"
LEGACY_MERGED_DIR = DATA_ROOT / "media-legacy"
FALLBACK_DIR = DATA_ROOT / "media-fallback"
CATALOG_DB = Path(os.environ.get("DIHI_CATALOG_DB", str(DATA_ROOT / "media-catalog.db"))).expanduser().resolve()
PLAYLIST_METADATA_DIR = Path(os.environ.get("DIHI_PLAYLIST_METADATA_DIR", str(DATA_ROOT / "playlists"))).expanduser().resolve()
SETTINGS_FILE = Path(os.environ.get("DIHI_SETTINGS_FILE", str(DATA_ROOT / "settings.json"))).expanduser().resolve()
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
_queue_scheduler_ready = threading.Event()


class _YTDlpLogCapture:
    """Keep useful yt-dlp diagnostics with a download attempt."""

    def __init__(self, video_id: str) -> None:
        self.video_id = video_id
        self.lines: list[str] = []

    def _add(self, message: object) -> None:
        text = str(message or "").strip()
        if not text:
            return
        with _lock:
            self.lines.append(text)
            del self.lines[:-40]
            detail = _download_details.setdefault(self.video_id, {"logs": []})
            detail.setdefault("logs", []).append(text)
            detail["logs"] = detail["logs"][-40:]

    def debug(self, message: object) -> None:
        self._add(message)

    def warning(self, message: object) -> None:
        self._add(message)

    def error(self, message: object) -> None:
        self._add(message)

# Playlist download tracking
_active_playlist_downloads: Set[str] = set()
_playlist_started_at: dict[str, float] = {}
_pause_requests: set[int] = set()
_cancel_requests: set[int] = set()
_remove_requests: set[int] = set()
_playlist_download_results: dict[str, str] = {}  # playlist_id -> "completed" | "failed"
_playlist_result_timestamps: dict[str, float] = {}
_playlist_history: dict[str, str] = {}
_playlist_history_timestamps: dict[str, float] = {}
_last_queue_log_message: Optional[str] = None
_queue_wakeup = threading.Event()
_cleanup_tasks: dict[str, dict] = {}
_cleanup_report_cache: tuple[float, dict] | None = None
_VERIFY_CACHE = Path(os.environ.get("DIHI_VERIFY_CACHE", str(DATA_ROOT / "media-verification.json"))).expanduser().resolve()
_STARTUP_READINESS = Path(os.environ.get("DIHI_STARTUP_READINESS", str(DATA_ROOT / "startup-readiness.json"))).expanduser().resolve()
_STARTUP_HISTORY = Path(os.environ.get("DIHI_STARTUP_HISTORY", str(DATA_ROOT / "startup-history.json"))).expanduser().resolve()
_PLAYLIST_INDEX_CACHE = Path(os.environ.get("DIHI_PLAYLIST_INDEX_CACHE", str(DATA_ROOT / "playlist-index-cache.json"))).expanduser().resolve()
_DERIVED_CACHE_DIR = Path(os.environ.get("DIHI_DERIVED_CACHE_DIR", str(DATA_ROOT / "derived-cache"))).expanduser().resolve()
_TAGS_CACHE = _DERIVED_CACHE_DIR / "tags.json"
_EXPORTS_CACHE = _DERIVED_CACHE_DIR / "exports.json"
_READINESS_VERSION = 2
_catalog_ready = threading.Event()
_available_catalog_ids: set[str] = set()
_playlist_index_cache_key: str | None = None
_playlist_index_cache_cards: list[dict] | None = None
_playlist_index_build_lock = threading.Lock()
_catalog_refresh_lock = threading.Lock()
_catalog_refresh_timer: threading.Timer | None = None
_system_status_cache: tuple[float, dict] | None = None
_catalog_refresh_state: dict = {
    "phase": "starting", "source": "startup", "started_at": None,
    "finished_at": None, "duration_seconds": None, "last_success": None,
    "last_error": None, "indexed_videos": None,
}


def _catalog_cache_fingerprint() -> dict | None:
    """Return the persisted catalog fingerprint without walking media."""
    try:
        payload = json.loads(_STARTUP_READINESS.read_text(encoding="utf-8"))
        if payload.get("version") == _READINESS_VERSION and CATALOG_DB.is_file():
            return payload.get("fingerprint")
    except (OSError, TypeError, ValueError):
        pass
    return None


def _read_derived_cache(path: Path) -> object | None:
    fingerprint = _catalog_cache_fingerprint()
    if fingerprint is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") == _READINESS_VERSION and payload.get("fingerprint") == fingerprint:
            return payload.get("value")
    except (OSError, TypeError, ValueError):
        pass
    return None


def _write_derived_cache(path: Path, value: object) -> None:
    fingerprint = _catalog_cache_fingerprint()
    if fingerprint is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": _READINESS_VERSION,
                                         "fingerprint": fingerprint,
                                         "saved_at": time.time(),
                                         "value": value}), encoding="utf-8")
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        app.logger.warning("Could not persist derived cache %s", path, exc_info=True)


def _stat_fingerprint(roots: tuple[Path, ...]) -> str:
    """Build a cheap change key from paths, sizes, and nanosecond mtimes."""
    digest = hashlib.sha256()
    for root in roots:
        root = Path(root)
        digest.update(str(root).encode())
        if not root.exists():
            digest.update(b"<missing>")
            continue
        if root.is_file():
            try:
                stat = root.stat()
                digest.update(f"<file>|{stat.st_size}|{stat.st_mtime_ns}".encode())
            except OSError:
                digest.update(b"<unreadable>")
            continue
        try:
            for current, directories, filenames in os.walk(root):
                directories.sort()
                filenames.sort()
                for name in [*directories, *filenames]:
                    path = Path(current) / name
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    digest.update(str(path.relative_to(root)).encode())
                    digest.update(f"|{stat.st_size}|{stat.st_mtime_ns}".encode())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def _directory_fingerprint(roots: tuple[Path, ...]) -> str:
    """Fast structural fingerprint for media trees.

    Media downloads change directory entries and directory mtimes. Avoiding
    per-file stat calls is important on USB/DrvFs startup; the authoritative
    catalog rebuild remains available through the explicit refresh action.
    """
    digest = hashlib.sha256()
    for root in roots:
        root = Path(root)
        digest.update(str(root).encode())
        if not root.exists():
            digest.update(b"<missing>")
            continue
        try:
            for current, directories, _filenames in os.walk(root):
                directories.sort()
                for name in directories:
                    path = Path(current) / name
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    digest.update(str(path.relative_to(root)).encode())
                    digest.update(f"|{stat.st_mtime_ns}".encode())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def _startup_fingerprint() -> dict[str, str]:
    return {
        "media": _directory_fingerprint((MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR)),
        "playlists": _stat_fingerprint((PLAYLIST_METADATA_DIR,)),
        "archive": _stat_fingerprint((CHECK_FILE,)),
    }


def _readiness_matches(fingerprint: dict[str, str]) -> bool:
    try:
        payload = json.loads(_STARTUP_READINESS.read_text(encoding="utf-8"))
        return (
            payload.get("version") == _READINESS_VERSION
            and payload.get("fingerprint") == fingerprint
            and CATALOG_DB.is_file()
        )
    except (OSError, TypeError, ValueError):
        return False


def _write_startup_readiness(fingerprint: dict[str, str], count: int) -> None:
    payload = {"version": _READINESS_VERSION, "fingerprint": fingerprint,
               "indexed_videos": count, "saved_at": time.time()}
    _STARTUP_READINESS.parent.mkdir(parents=True, exist_ok=True)
    temporary = _STARTUP_READINESS.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(_STARTUP_READINESS)


def _record_startup_run(*, source: str, started_at: float, finished_at: float,
                        phase: str, indexed_videos: int | None = None,
                        error: str | None = None) -> None:
    """Persist a compact startup/catalog timing history for the Status page."""
    run = {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(max(0.0, finished_at - started_at), 3),
        "source": source,
        "phase": phase,
        "indexed_videos": indexed_videos,
        "error": error,
    }
    try:
        try:
            history = json.loads(_STARTUP_HISTORY.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            history = []
        if not isinstance(history, list):
            history = []
        history.append(run)
        history = history[-30:]
        _STARTUP_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        temporary = _STARTUP_HISTORY.with_suffix(".tmp")
        temporary.write_text(json.dumps(history, indent=2), encoding="utf-8")
        temporary.replace(_STARTUP_HISTORY)
    except (OSError, TypeError, ValueError):
        app.logger.warning("Could not persist startup timing history", exc_info=True)


def _refresh_catalog_body(source: str = "startup") -> None:
    global _catalog_refresh_state
    started = time.perf_counter()
    started_at = time.time()
    with _lock:
        _catalog_refresh_state = {**_catalog_refresh_state, "phase": "validating fingerprint",
                                  "source": source, "started_at": started_at,
                                  "finished_at": None, "duration_seconds": None,
                                  "last_error": None}
    app.logger.info(
        "Startup: building media catalog (data=%s, strict=%s, legacy=%s, fallback=%s)",
        DATA_ROOT, MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR,
    )
    try:
        fingerprint = _startup_fingerprint()
        if _readiness_matches(fingerprint):
            app.logger.info("Startup: validated persisted media catalog; skipping full scan")
            finished_at = time.time()
            _record_startup_run(source=source, started_at=started_at, finished_at=finished_at,
                                phase="cache_hit", indexed_videos=None)
            with _lock:
                _catalog_refresh_state.update(
                    phase="ready (cache hit)", finished_at=finished_at,
                    duration_seconds=round(time.perf_counter() - started, 3),
                )
            _catalog_ready.set()
            return
        with _lock:
            _catalog_refresh_state["phase"] = "scanning filesystem"
        count = catalog.refresh([MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR], CATALOG_DB, CHECK_FILE,
                                PLAYLIST_METADATA_DIR)
        _write_startup_readiness(_startup_fingerprint(), count)
        finished_at = time.time()
        _record_startup_run(source=source, started_at=started_at, finished_at=finished_at,
                            phase="full_scan", indexed_videos=count)
        app.logger.info("Startup: media catalog ready; indexed %d videos in %.1fs",
                        count, time.perf_counter() - started)
        with _lock:
            _catalog_refresh_state.update(
                phase="ready", finished_at=time.time(),
                duration_seconds=round(time.perf_counter() - started, 3),
                last_success=time.time(), indexed_videos=count,
            )
        try:
            seeded = catalog.seed_settings_from_file(CATALOG_DB, SETTINGS_FILE)
            if seeded:
                app.logger.info("Restored %d settings from %s", seeded, SETTINGS_FILE)
        except Exception:
            app.logger.exception("Settings restore failed")
    except Exception as exc:
        finished_at = time.time()
        _record_startup_run(source=source, started_at=started_at, finished_at=finished_at,
                            phase="error", error=str(exc))
        with _lock:
            _catalog_refresh_state.update(
                phase="error", finished_at=time.time(),
                duration_seconds=round(time.perf_counter() - started, 3),
                last_error=str(exc),
            )
        app.logger.exception("Startup: media catalog refresh failed after %.1fs",
                             time.perf_counter() - started)
    finally:
        _catalog_ready.set()


def _refresh_catalog(source: str | None = None) -> None:
    if not _catalog_refresh_lock.acquire(blocking=False):
        app.logger.info("Catalog refresh already running")
        return
    try:
        with _lock:
            refresh_source = source or str(_catalog_refresh_state.get("source") or "startup")
        _refresh_catalog_body(refresh_source)
    finally:
        _catalog_refresh_lock.release()


def _request_catalog_refresh(reason: str = "change", delay: float = 1.5) -> None:
    """Coalesce bursts of post-download catalog refresh requests."""
    global _catalog_refresh_timer
    with _lock:
        if _catalog_refresh_timer and _catalog_refresh_timer.is_alive():
            app.logger.info("Catalog refresh already scheduled (%s)", reason)
            return
        _catalog_refresh_state["source"] = reason
        app.logger.info("Scheduling catalog refresh in %.1fs (%s)", delay, reason)
        _catalog_refresh_timer = threading.Timer(delay, _refresh_catalog)
        _catalog_refresh_timer.daemon = True
        _catalog_refresh_timer.start()


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


def _save_playlist_metadata(playlist_id: str, title: str, webpage_url: str,
                            members: list[dict]) -> None:
    """Persist preflight membership independently of the SQLite catalog."""
    PLAYLIST_METADATA_DIR.mkdir(parents=True, exist_ok=True)
    path = PLAYLIST_METADATA_DIR / f"{playlist_id}.info.json"
    payload = {
        "_type": "playlist", "id": playlist_id, "title": title or playlist_id,
        "webpage_url": webpage_url, "saved_at": time.time(), "entries": members,
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _record_playlist_membership_best_effort(playlist_id: str, title: str,
                                            webpage_url: str | None,
                                            members: list[dict]) -> None:
    """Keep the descriptor usable when SQLite is busy with a catalog scan."""
    try:
        catalog.record_playlist_membership(CATALOG_DB, playlist_id, title, webpage_url, members)
    except (sqlite3.Error, OSError) as exc:
        app.logger.info("Catalog busy; retained playlist descriptor for %s: %s", playlist_id, exc)


def _fast_playlist_snapshot(playlist_id: str) -> tuple[dict, list[dict]] | None:
    """Read saved playlist membership without waiting on the catalog writer."""
    path = PLAYLIST_METADATA_DIR / f"{playlist_id}.info.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    entries = payload.get("entries") or []
    if not isinstance(entries, list):
        entries = []
    members = [entry for entry in entries if isinstance(entry, dict) and entry.get("video_id")]
    return ({"playlist_id": playlist_id, "title": payload.get("title") or playlist_id,
             "webpage_url": payload.get("webpage_url"), "updated_at": payload.get("saved_at")}, members)


def _fast_catalog_videos(video_ids: Optional[set[str]] = None) -> dict[str, dict]:
    """Best-effort catalog enrichment with a sub-second lock wait."""
    try:
        database_uri = f"file:{CATALOG_DB}?mode=ro&immutable=1"
        with sqlite3.connect(database_uri, uri=True, timeout=0.1) as db:
            if video_ids:
                placeholders = ",".join("?" for _ in video_ids)
                rows = db.execute(
                    f"SELECT video_id, title, files_json FROM videos "
                    f"WHERE video_id IN ({placeholders}) "
                    "ORDER BY CASE source_root WHEN 'merged' THEN 0 WHEN 'legacy' THEN 1 ELSE 2 END",
                    tuple(video_ids),
                ).fetchall()
            else:
                rows = db.execute("SELECT video_id, title, files_json FROM videos ORDER BY CASE source_root WHEN 'merged' THEN 0 WHEN 'legacy' THEN 1 ELSE 2 END").fetchall()
    except (sqlite3.Error, OSError):
        return {}
    videos = {}
    for video_id, title, raw_files in rows:
        if video_id in videos:
            continue
        try:
            files = catalog._slim_files(json.loads(raw_files or "{}"))
        except (TypeError, ValueError):
            files = {}
        videos[video_id] = {"video_id": video_id, "title": title or video_id, "files": files}
    return videos


def _descriptor_playlist_cards_body() -> list[dict]:
    """Build a non-blocking playlist list from saved descriptor JSON."""
    global _available_catalog_ids, _playlist_index_cache_key, _playlist_index_cache_cards
    started = time.perf_counter()
    cache_key = json.dumps({
        "descriptors": _stat_fingerprint((PLAYLIST_METADATA_DIR,)),
        "media": _directory_fingerprint((MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR)),
    }, sort_keys=True)
    if _playlist_index_cache_key == cache_key and _playlist_index_cache_cards is not None:
        return _playlist_index_cache_cards
    try:
        cached = json.loads(_PLAYLIST_INDEX_CACHE.read_text(encoding="utf-8"))
        if cached.get("key") == cache_key:
            cards = cached.get("cards")
            available = cached.get("available_catalog_ids")
            if isinstance(cards, list) and isinstance(available, list):
                _playlist_index_cache_key = cache_key
                _playlist_index_cache_cards = cards
                _available_catalog_ids = set(str(item) for item in available)
                app.logger.info("Playlist index cache hit: %d playlists", len(cards))
                return cards
    except (OSError, TypeError, ValueError):
        pass
    rows = []
    wanted = set()
    for path in sorted(PLAYLIST_METADATA_DIR.glob("*.info.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("_type") != "playlist":
            continue
        playlist_id = str(payload.get("id") or path.stem).strip()
        if not PLAYLIST_ID_RE.match(playlist_id):
            continue
        entries = [item for item in (payload.get("entries") or [])
                   if isinstance(item, dict) and item.get("video_id")]
        rows.append((payload, playlist_id, entries))
        wanted.update(str(item["video_id"]) for item in entries)

    # Do not walk the bind-mounted media tree here. On a large USB/DrvFs
    # archive that turns the playlist index request into a minutes-long scan.
    # The catalog already contains the per-file snapshot needed for this
    # count, and immutable read-only access does not contend with its writer.
    available_ids = set()
    if wanted:
        database_uri = f"file:{CATALOG_DB}?mode=ro&immutable=1"
        try:
            with sqlite3.connect(database_uri, uri=True, timeout=0.1) as db:
                catalog_rows = db.execute("SELECT video_id, files_json FROM videos").fetchall()
            for video_id, raw_files in catalog_rows:
                if video_id not in wanted:
                    continue
                try:
                    files = json.loads(raw_files or "{}")
                except (TypeError, ValueError):
                    files = {}
                kinds = {str(item.get("kind") or "") for item in files.values()
                         if isinstance(item, dict)}
                if "video" in kinds and "audio" in kinds:
                    available_ids.add(video_id)
        except (sqlite3.Error, OSError):
            pass

    cards = []
    for payload, playlist_id, entries in rows:
        available = sum(1 for item in entries if item["video_id"] in available_ids)
        cards.append({
            "playlist_id": playlist_id,
            "title": payload.get("title") or playlist_id,
            "webpage_url": payload.get("webpage_url") or f"https://www.youtube.com/playlist?list={playlist_id}",
            "video_count": len(entries),
            "available_count": available,
            "missing_count": max(0, len(entries) - available),
            "updated_at": payload.get("saved_at"),
        })
    cards.sort(key=lambda item: (-item["missing_count"], str(item["title"]).lower(), item["playlist_id"]))
    _available_catalog_ids = available_ids
    _playlist_index_cache_key = cache_key
    _playlist_index_cache_cards = cards
    try:
        _PLAYLIST_INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
        temporary = _PLAYLIST_INDEX_CACHE.with_name(
            f".{_PLAYLIST_INDEX_CACHE.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(json.dumps({"key": cache_key, "cards": cards,
                                         "available_catalog_ids": sorted(available_ids)},
                                        ensure_ascii=False), encoding="utf-8")
        temporary.replace(_PLAYLIST_INDEX_CACHE)
    except OSError:
        app.logger.warning("Could not persist playlist index cache", exc_info=True)
    app.logger.info("Playlist index built: %d descriptors, %d members, %d locally complete in %.1fs",
                    len(cards), len(wanted), len(available_ids), time.perf_counter() - started)
    return cards


def _descriptor_playlist_cards() -> list[dict]:
    """Serialize the expensive playlist-index build within this worker."""
    with _playlist_index_build_lock:
        return _descriptor_playlist_cards_body()

def _recover_playlist_members_from_media(playlist_id: str) -> tuple[str, str, list[dict]] | None:
    """Recover membership from downloaded video info sidecars when a descriptor is empty."""
    roots = (MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR)
    found: dict[str, dict] = {}
    title = playlist_id
    webpage_url = f"https://www.youtube.com/playlist?list={playlist_id}"
    for root in roots:
        for channel_dir in root.iterdir() if root.is_dir() else []:
            if not channel_dir.is_dir():
                continue
            for video_dir in channel_dir.iterdir():
                if not video_dir.is_dir():
                    continue
                info = catalog._info(video_dir)
                if str(info.get("playlist_id") or "").strip() != playlist_id:
                    continue
                video_id = str(info.get("id") or video_dir.name).strip()
                if not YOUTUBE_ID_RE.match(video_id) or video_id in found:
                    continue
                title = str(info.get("playlist_title") or title)
                webpage_url = str(info.get("playlist_webpage_url") or webpage_url)
                found[video_id] = {
                    "video_id": video_id,
                    "playlist_index": info.get("playlist_index"),
                    "title": info.get("title") or video_id,
                    "video_url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                }
    if not found:
        return None
    members = sorted(found.values(), key=lambda item: (item["playlist_index"] is None, item["playlist_index"] or 0, item["video_id"]))
    return title, webpage_url, members


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


class _DownloadCancelled(Exception):
    """Internal signal used to stop an in-flight yt-dlp transfer."""


def _progress_hook(video_id: str, queue_item_id: int | None = None):
    """Create a yt-dlp hook that keeps concise progress text for the UI."""
    def hook(event: dict) -> None:
        if queue_item_id:
            with _lock:
                if queue_item_id in _cancel_requests:
                    raise _DownloadCancelled("download stopped by user")
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
        ("video unavailable", "not_found", False), ("this video is not available", "not_found", False),
        ("removed by the uploader", "not_found", False), ("sign in", "login_required", True),
        ("did not produce both", "incomplete", True),
    ]
    for needle, reason, retryable in patterns:
        if needle in text:
            return reason, retryable
    return "unknown", True


def _permanent_failure(video_id: str) -> tuple[str, str] | None:
    try:
        with sqlite3.connect(CATALOG_DB) as db:
            row = db.execute("SELECT reason, raw_error FROM download_attempts WHERE video_id=? ORDER BY finished_at DESC LIMIT 1", (video_id,)).fetchone()
        if row and row[0] in {"not_found", "private", "members_only", "region_blocked"}:
            return str(row[0]), str(row[1] or row[0])
    except sqlite3.Error:
        pass
    return None


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
    remaining_playlists = len(active_playlists)
    pending_videos = pending_playlists = 0
    try:
        persisted = catalog.queue_items(CATALOG_DB, 2000)
        with _lock:
            stopping_ids = {int(item["id"]) for item in persisted
                            if item["status"] == "paused" and int(item["id"]) in _cancel_requests}
        running_ids = {(item["target"], item["kind"]): int(item["id"])
                       for item in persisted
                       if item["status"] == "running" or int(item["id"]) in stopping_ids}
        for entry in active:
            queue_id = running_ids.get((entry["id"], entry["kind"]))
            if queue_id is not None:
                entry["queue_id"] = queue_id
                with _lock:
                    entry["pause_requested"] = queue_id in _pause_requests
                    if queue_id in _cancel_requests:
                        entry["status"] = "stopping"
                        entry["stopping"] = True
        pending_videos = sum(1 for item in persisted if item["kind"] == "video" and item["status"] in {"running", "pending", "paused"})
        pending_playlists = sum(1 for item in persisted if item["kind"] == "playlist" and item["status"] in {"running", "pending", "paused"})
        remaining_videos = max(remaining_videos, pending_videos)
        remaining_playlists = max(remaining_playlists, pending_playlists)
    except (sqlite3.Error, OSError, ValueError):
        pass
    queue = _queue_summary_from_active(remaining_videos, remaining_playlists)
    if not _catalog_ready.is_set():
        queue["scheduler_state"] = "catalog_starting"
        queue["scheduler_message"] = "Preparing the media catalog; queued items will start when the scan finishes."
    elif not _queue_scheduler_ready.is_set():
        queue["scheduler_state"] = "starting"
        queue["scheduler_message"] = "Starting the download scheduler; queued items will begin shortly."
    else:
        queue["scheduler_state"] = "ready"
        queue["scheduler_message"] = ""
    queue["pending_videos"] = max(0, pending_videos - len(active_videos))
    queue["pending_playlists"] = max(0, pending_playlists - len(active_playlists))
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
            "active_videos": len(active_videos),
            "active_playlists": len(active_playlists),
            "max_videos": _queue_limit("video"),
            "max_playlists": _queue_limit("playlist"),
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
    paused = False
    error_text = ""
    started_at = time.time()
    log_capture = _YTDlpLogCapture(video_id)
    try:
        result_code = getvidyt.download_youtube(
            video_id,
            audio_meta=True,
            cookies_browser=cookies_browser,
            extra_opts={"progress_hooks": [_progress_hook(video_id, queue_item_id)],
                        "download_archive": None if (
                            _media_needs_archive_retry(video_id)
                            or _media_needs_sidecar_retry(video_id)
                        ) else str(CHECK_FILE),
                        "logger": log_capture},
        )
        # Give filesystem time to sync archive.txt
        time.sleep(0.5)
        # Force cache refresh and check if video is now in archive
        global _cached_mtime
        with _lock:
            _cached_mtime = None  # Force refresh
        _ensure_cache()
        # The catalog may still contain the pre-download file list. Read the
        # live strict/legacy directory first so freshly created video/audio
        # outputs are visible to the completion check immediately.
        media = _scan_single_video(video_id) or _resolve_media_by_video_id(video_id) or {}
        media_files = media.get("files") or {}
        has_video = bool(media_files.get("video"))
        has_audio = bool(media_files.get("audio"))
        with _lock:
            # Retries of incomplete archived items intentionally disable
            # yt-dlp's archive option, so archive membership is not available
            # as a completion signal here. The playable outputs are the
            # authoritative result of this download.
            # A playable pair is authoritative. yt-dlp may return non-zero
            # after the media is complete when an optional postprocessor
            # rejects a kept sidecar (notably thumbnail embedding on WebM).
            # Treat that as complete instead of retrying into fallback forever.
            success = has_video and has_audio
            paused = bool(queue_item_id and queue_item_id in _pause_requests)
            cancelled = bool(queue_item_id and queue_item_id in _cancel_requests)
            if cancelled:
                paused = queue_item_id not in _remove_requests
                success = False
        if result_code:
            error_text = f"yt-dlp returned exit code {result_code}"
        elif not success:
            error_text = "download did not produce both a playable video and audio file"
            if log_capture.lines:
                error_text += "; " + " | ".join(log_capture.lines[-5:])
        if not success and _media_needs_archive_retry(video_id):
            _remove_from_archive(video_id)
        elif success:
            _add_to_archive(video_id)
    except _DownloadCancelled as e:
        error_text = str(e)
        with _lock:
            paused = bool(queue_item_id and queue_item_id not in _remove_requests)
        success = False
    except Exception as e:
        error_text = str(e)
        app.logger.exception("Download failed for %s: %s", video_id, e)
        success = False
    finally:
        with _lock:
            _active_downloads.discard(video_id)
            # Store result for status endpoint
            removed = bool(queue_item_id and queue_item_id in _remove_requests)
            result = "cancelled" if removed else ("paused" if paused else ("completed" if success else "failed"))
            _pause_requests.discard(queue_item_id)
            _cancel_requests.discard(queue_item_id)
            _remove_requests.discard(queue_item_id)
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
        if removed:
            if queue_item_id:
                catalog.queue_set_status(CATALOG_DB, queue_item_id, "cancelled", error="removed by user", finished_at=time.time())
        elif paused:
            if queue_item_id:
                catalog.queue_set_status(CATALOG_DB, queue_item_id, "paused", error="paused by user", finished_at=None)
        elif success:
            _request_catalog_refresh("completed download")
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
                        "no_warnings": True, "ignoreerrors": True,
                        # Preflight must list every playlist entry, including
                        # videos already in the download archive. yt-dlp drops
                        # archived entries from flat-preflight results, which
                        # used to shrink membership and zero out the
                        # locally-available count for playlists with
                        # previously downloaded members.
                        "download_archive": None},
        )
        with getvidyt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        entries = info.get("entries") or []
        if not entries:
            recovered = _recover_playlist_members_from_media(playlist_id)
            if recovered:
                title, webpage_url, members = recovered
                _save_playlist_metadata(playlist_id, title, webpage_url, members)
                _record_playlist_membership_best_effort(playlist_id, title, webpage_url, members)
                return members
            raise RuntimeError("YouTube returned no playlist members; it may be private, unavailable, or require authentication")
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
        playlist_title = str(info.get("title") or info.get("playlist_title") or playlist_id)
        playlist_url = str(info.get("webpage_url") or f"https://www.youtube.com/playlist?list={playlist_id}")
        _save_playlist_metadata(playlist_id, playlist_title, playlist_url, members)
        _record_playlist_membership_best_effort(playlist_id, playlist_title, playlist_url, members)
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
        recovered = _recover_playlist_members_from_media(playlist_id)
        if recovered:
            title, webpage_url, members = recovered
            _save_playlist_metadata(playlist_id, title, webpage_url, members)
            _record_playlist_membership_best_effort(playlist_id, title, webpage_url, members)
            return members
        app.logger.warning("Playlist preflight failed for %s; continuing download: %s", playlist_id, exc)
        return None


def _playlist_download_worker(playlist_id: str, queue_item_id: int | None = None) -> None:
    """Download all videos from a YouTube playlist via getvidyt."""
    paused = False
    success = False
    try:
        members = _prepare_playlist_membership(playlist_id)
    except Exception:
        app.logger.exception("Playlist refresh failed for %s", playlist_id)
        return jsonify(ok=False, error="playlist refresh failed; check the server log and try again"), 503
    try:
        if members is not None:
            # Download each child independently. This keeps a strict success
            # from being downloaded again when a different playlist child
            # needs the fallback format.
            rc = 0
            for member in members:
                if queue_item_id and (_pause_requested(queue_item_id) or _cancel_requested(queue_item_id)):
                    paused = True
                    break
                child_rc = getvidyt.download_youtube(
                    member["video_id"],
                    audio_meta=True,
                    extra_opts={"progress_hooks": [_progress_hook(playlist_id, queue_item_id)],
                                "download_archive": None if (
                                    _media_needs_archive_retry(member["video_id"])
                                    or _media_needs_sidecar_retry(member["video_id"])
                                ) else str(CHECK_FILE)},
                )
                if child_rc:
                    rc = child_rc
                if queue_item_id and (_pause_requested(queue_item_id) or _cancel_requested(queue_item_id)):
                    paused = True
                    break
        else:
            # If preflight cannot read the playlist, do not retry the entire
            # playlist into the fallback tree; that can duplicate successes.
            rc = getvidyt.download_youtube(
                playlist_id,
                audio_meta=True,
                best_fallback=False,
                extra_opts={"progress_hooks": [_progress_hook(playlist_id, queue_item_id)]},
            )
        # Force cache refresh so status can report archive contents
        global _cached_mtime
        with _lock:
            _cached_mtime = None
        _ensure_cache()
        with _lock:
            cancelled = bool(queue_item_id and queue_item_id in _cancel_requests)
        success = rc == 0 and not paused and not cancelled
        if cancelled:
            paused = queue_item_id not in _remove_requests
    except _DownloadCancelled:
        with _lock:
            paused = bool(queue_item_id and queue_item_id not in _remove_requests)
        success = False
    except Exception as e:
        app.logger.exception("Playlist download failed for %s: %s", playlist_id, e)
        success = False
    finally:
        with _lock:
            _active_playlist_downloads.discard(playlist_id)
            removed = bool(queue_item_id and queue_item_id in _remove_requests)
            result = "cancelled" if removed else ("paused" if paused else ("completed" if success else "failed"))
            _pause_requests.discard(queue_item_id)
            _cancel_requests.discard(queue_item_id)
            _remove_requests.discard(queue_item_id)
            finished_at = time.time()
            _playlist_download_results[playlist_id] = result
            _playlist_result_timestamps[playlist_id] = finished_at
            _playlist_history[playlist_id] = result
            _playlist_history_timestamps[playlist_id] = finished_at
            if queue_item_id:
                if removed:
                    catalog.queue_set_status(CATALOG_DB, queue_item_id, "cancelled", error="removed by user", finished_at=finished_at)
                elif paused:
                    catalog.queue_set_status(CATALOG_DB, queue_item_id, "paused", error="paused by user", finished_at=None)
                else:
                    catalog.queue_set_status(CATALOG_DB, queue_item_id, result, finished_at=finished_at)
            _cleanup_old_playlist_results()
            _cleanup_old_playlist_history()
            _log_queue_state_locked()


def _pause_requested(queue_item_id: int | None) -> bool:
    if not queue_item_id:
        return False
    with _lock:
        return queue_item_id in _pause_requests


def _cancel_requested(queue_item_id: int | None) -> bool:
    if not queue_item_id:
        return False
    with _lock:
        return queue_item_id in _cancel_requests


def _start_queue_item(item: dict) -> bool:
    """Start one persisted queue item if its in-memory concurrency slot is free."""
    target, kind, item_id = item["target"], item["kind"], int(item["id"])
    with _lock:
        active = _active_downloads if kind == "video" else _active_playlist_downloads
        limit = _queue_limit(kind)
        if target in active or len(active) >= limit:
            return False
        active.add(target)
        started = time.time()
        (_download_started_at if kind == "video" else _playlist_started_at)[target] = started
        _download_details[target] = {"phase": "queued", "percent": 0, "filename": "", "files": {}, "logs": []}
        _log_queue_state_locked()
    catalog.queue_set_status(CATALOG_DB, item_id, "running", started_at=started)
    app.logger.info("Queue start: id=%d kind=%s target=%s", item_id, kind, target)
    worker = _download_worker if kind == "video" else _playlist_download_worker
    args = (target, item.get("cookies_browser"), item_id) if kind == "video" else (target, item_id)
    threading.Thread(target=worker, args=args, daemon=True).start()
    return True


def _recover_running_queue_items() -> int:
    """Pause jobs interrupted by a server restart instead of auto-resuming them."""
    recovered = 0
    for stale in catalog.queue_items(CATALOG_DB, 100):
        if stale["status"] == "running":
            catalog.queue_set_status(
                CATALOG_DB,
                int(stale["id"]),
                "paused",
                error="paused after server restart",
            )
            recovered += 1
    return recovered


def _queue_scheduler() -> None:
    app.logger.info("Startup: download queue scheduler starting")
    recovered = False
    while True:
        try:
            now = time.time()
            if not recovered:
                recovered_count = _recover_running_queue_items()
                _repair_queue_kinds(catalog.queue_items(CATALOG_DB, 100))
                app.logger.info("Startup: queue scheduler ready; recovered %d interrupted item(s)", recovered_count)
                recovered = True
                _queue_scheduler_ready.set()
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
    if status == "pending":
        # The schema allows a pending and paused row for the same target, so
        # this case does not raise IntegrityError. Promote the paused row
        # first so an explicit immediate request cannot leave it stranded.
        for item in catalog.queue_items(CATALOG_DB, 2000):
            if item["target"] == target and item["kind"] == kind and item["status"] == "paused":
                catalog.queue_set_status(
                    CATALOG_DB, int(item["id"]), "pending",
                    scheduled_at=scheduled_at or time.time(),
                )
                _queue_wakeup.set()
                return int(item["id"]), True
    try:
        item_id = catalog.queue_add(CATALOG_DB, target, kind, scheduled_at, cookies_browser, status)
        _queue_wakeup.set()
        return item_id, False
    except sqlite3.IntegrityError:
        for item in catalog.queue_items(CATALOG_DB, 2000):
            if item["target"] == target and item["kind"] == kind and item["status"] in {"pending", "paused"}:
                # An explicit immediate request should wake an item that was
                # previously held in the paused queue (for example, from the
                # per-song Download button on the playlist page).
                if status == "pending" and item["status"] == "paused":
                    catalog.queue_set_status(
                        CATALOG_DB, int(item["id"]), "pending",
                        scheduled_at=scheduled_at or time.time(),
                    )
                    _queue_wakeup.set()
                return int(item["id"]), True
        raise


def _repair_queue_kinds(items: list[dict]) -> list[dict]:
    """Correct old queue rows created before playlist IDs were detected."""
    for item in items:
        target = str(item.get("target") or "")
        expected = "video" if YOUTUBE_ID_RE.match(target) else "playlist"
        if item.get("kind") == "video" and expected == "playlist" and item.get("status") != "running":
            catalog.queue_set_kind(CATALOG_DB, int(item["id"]), expected)
            item["kind"] = expected
    return items


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
    entry = {
        "name": path.name,
        "url": f"{media_prefix}/{quote(channel_id, safe='')}/{quote(video_id, safe='')}/{quote(path.name, safe='')}",
        "kind": _file_kind(path),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }
    if path.suffix.lower() == ".m4a" and path.name.endswith(".out.m4a") and not _SIDECAR_RE.search(path.name):
        entry.update(delivery="generated", processing="remuxed/tagged")
    elif _SIDECAR_RE.search(path.name):
        entry["delivery"] = "downloaded"
    return entry


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


def _is_playlist_dir(directory: Path) -> bool:
    """Playlist metadata is not a media item and must not get media checks."""
    info, _error = _load_info_json(directory)
    return bool(info and info.get("_type") == "playlist")


def _scan_video_dir(channel_id: str, video_dir: Path, media_prefix: str, source_root: str) -> Optional[dict]:
    """Build the library dict for one ``<channel_id>/<video_id>/`` directory.

    Returns None for playlist descriptor directories. Extracted from
    _scan_library so single-video lookups can reuse the exact same shape
    without walking the whole merged tree.
    """
    video_id = video_dir.name
    info, _info_error = _load_info_json(video_dir)
    if info and info.get("_type") == "playlist":
        return None
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
    return {
        "video_id": video_id,
        "channel_id": channel_id,
        "source_root": source_root,
        "title": title or video_id,
        "date": date,
        "artist": (info or {}).get("artist") or (info or {}).get("creator")
                  or (info or {}).get("uploader") or (info or {}).get("channel") or channel_id,
        "uploader": (info or {}).get("uploader") or (info or {}).get("channel") or channel_id,
        "track": (info or {}).get("track") or "",
        "files": files,
        "details": {
            "files": detail_files,
            "metadata": {
                "description": description,
                "info_json": {k: _safe_metadata_value(v) for k, v in (info or {}).items()},
            },
        },
    }


def _library_fingerprint() -> tuple:
    """Cheap stat-only snapshot of the channel/video directory structure.

    New downloads add directories/files (bumping dir mtimes) and cleanup
    deletes or moves them, so any add/delete changes this fingerprint without
    reading a single file. In-place content modifications can be missed, but
    those only happen mid-download while files are incomplete anyway.
    """
    parts = []
    for root in (MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR):
        try:
            channels = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            continue
        for channel_dir in channels:
            try:
                is_dir = channel_dir.is_dir()
            except OSError:
                continue
            if not is_dir:
                continue
            try:
                videos = sorted(video_dir.name for video_dir in channel_dir.iterdir()
                                if video_dir.is_dir())
            except OSError:
                videos = []
            try:
                mtime = channel_dir.stat().st_mtime_ns
            except OSError:
                mtime = 0
            parts.append((channel_dir.name, tuple(videos), mtime))
    return tuple(parts)


_LIBRARY_CACHE: dict = {"fingerprint": None, "videos": []}


def _scan_library_cached() -> list[dict]:
    """Return the library scan, reusing the cached result when nothing was
    added or deleted since the last scan. Thread-safe; each Gunicorn worker
    holds its own cache (see Coding Caveats about multi-worker state)."""
    fingerprint = _library_fingerprint()
    with _lock:
        if _LIBRARY_CACHE["fingerprint"] == fingerprint:
            return _LIBRARY_CACHE["videos"]
    videos = _scan_library()
    with _lock:
        _LIBRARY_CACHE["fingerprint"] = fingerprint
        _LIBRARY_CACHE["videos"] = videos
    return videos


def _scan_library() -> list[dict]:
    """Walk primary, legacy, and fallback trees, preferring strict copies.

    bestfallback entries are lower-quality (non-strict formats) kept when the
    strict download failed. They are shown and playable so nothing downloaded
    is invisible, but resolve stays strict-only so the extension and retry
    flows keep upgrading them to strict copies.
    """
    videos = []
    seen: Set[str] = set()
    for root, source_root, media_prefix in ((MERGED_DIR, "merged", "/media"),
                                            (LEGACY_MERGED_DIR, "legacy", "/media-legacy"),
                                            (FALLBACK_DIR, "bestfallback", "/media-fallback")):
      if not root.exists():
        continue
      for channel_dir in sorted(root.iterdir()):
        if not channel_dir.is_dir():
            continue
        channel_id = channel_dir.name
        for video_dir in sorted(channel_dir.iterdir()):
            if not video_dir.is_dir():
                continue
            video_id = video_dir.name
            if video_id in seen:
                continue
            video = _scan_video_dir(channel_id, video_dir, media_prefix, source_root)
            if video is None:
                continue
            seen.add(video_id)
            videos.append(video)
    return videos


def _scan_single_video(video_id: str) -> Optional[dict]:
    """Look up one video's directory directly, preferring the primary copy.

    Same shape and precedence as _scan_library, but stats channel folders
    instead of reading every video on disk. Used by the resolve endpoint
    (hit on every extension page visit) and the cleanup retry loop.

    Strict copies only: fallback videos are intentionally invisible here so
    the extension and retry flows keep upgrading them to strict downloads.
    """
    if not YOUTUBE_ID_RE.match(video_id):
        return None
    for root, source_root, media_prefix in ((MERGED_DIR, "merged", "/media"),
                                            (LEGACY_MERGED_DIR, "legacy", "/media-legacy")):
        if not root.is_dir():
            continue
        for channel_dir in sorted(root.iterdir()):
            if not channel_dir.is_dir():
                continue
            video_dir = channel_dir / video_id
            if not video_dir.is_dir():
                continue
            video = _scan_video_dir(channel_dir.name, video_dir, media_prefix, source_root)
            if video is not None:
                return video
    return None


def _resolve_media_by_video_id(video_id: str) -> Optional[dict]:
    # Catalog-first: single indexed lookup, no channel-directory walk. Falls
    # back to the filesystem scan during the first-startup window or when the
    # indexed row is stale/missing.
    try:
        record = catalog.resolve_video_by_id(CATALOG_DB, video_id)
    except Exception:
        record = None
    if record:
        channel_id = str(record.get("channel_id") or "")
        source_root = str(record.get("source_root") or "")
        roots = [(MERGED_DIR, "merged", "/media"), (LEGACY_MERGED_DIR, "legacy", "/media-legacy")]
        roots.sort(key=lambda item: 0 if item[1] == source_root else 1)
        for root, name, _prefix in roots:
            try:
                live_dir = root / channel_id / video_id
                is_dir = live_dir.is_dir()
            except (OSError, ValueError):
                continue
            if not is_dir or _is_playlist_dir(live_dir):
                continue
            try:
                if name == source_root:
                    files_full = record.get("files") or {}
                else:
                    files_full = catalog._files(live_dir, channel_id, video_id, name)
                    source_root = name
            except (OSError, ValueError):
                continue
            files = catalog._slim_files(files_full if isinstance(files_full, dict) else {})
            player_url = files.get("video") or files.get("audio")
            if not player_url:
                break
            detail_files = []
            if isinstance(files_full, dict):
                for entry_name in sorted(files_full):
                    entry = files_full[entry_name]
                    if not isinstance(entry, dict) or not entry.get("url"):
                        continue
                    detail_files.append({
                        "name": entry.get("name") or entry_name,
                        "url": entry.get("url"),
                        "kind": _file_kind(Path(entry.get("name") or entry_name)),
                        "size": entry.get("size", 0),
                        "mtime": entry.get("mtime", 0),
                    })
            metadata = record.get("metadata") or {}
            description = str(metadata.get("description") or "")
            return {
                "video_id": video_id,
                "channel_id": channel_id,
                "source_root": source_root,
                "title": record.get("title") or video_id,
                "date": record.get("date"),
                "artist": record.get("artist") or record.get("uploader") or channel_id,
                "uploader": record.get("uploader") or channel_id,
                "track": record.get("track") or "",
                "files": files,
                "details": {"files": detail_files, "metadata": {"description": description}},
                "player_url": player_url,
                "player_kind": "video" if files.get("video") else "audio" if files.get("audio") else None,
            }
    video = _scan_single_video(video_id)
    if video is None:
        return None
    files = video.get("files") or {}
    player_url = files.get("video") or files.get("audio")
    details = video.get("details") or {}
    detail_files = details.get("files") or []
    description = (details.get("metadata") or {}).get("description", "")
    # The full info.json (often hundreds of KB of formats) stays behind
    # /api/media/details/<channel>/<video>; the /video/ page loads it
    # on click, and the extension only needs the player URL.
    return {
        "video_id": video.get("video_id"),
        "channel_id": video.get("channel_id"),
        "source_root": video.get("source_root"),
        "title": video.get("title"),
        "date": video.get("date"),
        "artist": video.get("artist") or video.get("uploader") or video.get("channel_id"),
        "uploader": video.get("uploader") or video.get("channel_id"),
        "track": video.get("track") or "",
        "files": files,
        "details": {"files": detail_files, "metadata": {"description": description}},
        "player_url": player_url,
        "player_kind": "video" if files.get("video") else "audio" if files.get("audio") else None,
    }


def _slim_video(video: dict) -> dict:
    """Card fields for list responses: everything the grid, search/sort, and
    playlist views need, without the heavy per-video details.

    Descriptions and full info.json stay available per video via
    /api/media/details/<channel>/<video> (used lazily by the UI) and via
    /api/media/resolve/<video> (used by the /video/ page and extension).
    """
    return {
        "video_id": video.get("video_id"),
        "channel_id": video.get("channel_id"),
        "source_root": video.get("source_root"),
        "title": video.get("title"),
        "date": video.get("date"),
        "artist": video.get("artist") or video.get("uploader") or video.get("channel_id"),
        "uploader": video.get("uploader") or video.get("channel_id"),
        "track": video.get("track") or "",
        "files": video.get("files") or {},
    }


def _media_needs_sidecar_retry(video_id: str) -> bool:
    """Return whether an existing media item is missing downloadable sidecars."""
    for root in (MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR):
        for channel_dir in root.iterdir() if root.is_dir() else []:
            directory = channel_dir / video_id
            if not directory.is_dir() or _is_playlist_dir(directory):
                continue
            names = {path.name for path in directory.iterdir() if path.is_file()}
            return not (any(name.endswith(".out.info.json") for name in names)
                        and any(name.endswith(".out.formats.json") for name in names)
                        and any(name.endswith((".out.webp", ".out.png", ".out.jpg")) for name in names)
                        and any(name.endswith((".out.description", ".out.en.vtt", ".out.en-orig.vtt")) for name in names))
    return False


def _media_needs_archive_retry(video_id: str) -> bool:
    """Return whether an archive entry lacks both strict playable outputs.

    yt-dlp skips an archived ID before inspecting the filesystem. Bypass the
    archive when a previous attempt recorded the ID but left an incomplete
    strict/legacy media directory, or when only a fallback copy exists.
    """
    media = _scan_single_video(video_id)
    files = (media or {}).get("files") or {}
    return not (files.get("video") and files.get("audio"))


def _remove_from_archive(video_id: str, archive: Path | None = None) -> bool:
    """Remove one incomplete video from yt-dlp's archive and refresh cache."""
    path = (archive or CHECK_FILE).expanduser().resolve()
    if not path.is_file():
        return False
    temporary: Path | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
        kept = [line for line in lines if _parse_archive_line(line) != video_id]
        if len(kept) == len(lines):
            return False
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text("".join(kept), encoding="utf-8")
        os.replace(temporary, path)
        if path == CHECK_FILE:
            global _cached_mtime
            with _lock:
                _cached_mtime = None
                _cached_ids.discard(video_id)
        return True
    except OSError:
        app.logger.exception("Could not remove %s from archive %s", video_id, path)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _add_to_archive(video_id: str, archive: Path | None = None) -> bool:
    """Record a successfully completed video in yt-dlp's archive."""
    path = (archive or CHECK_FILE).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True) if path.is_file() else []
        if any(_parse_archive_line(line) == video_id for line in lines):
            return False
        with path.open("a", encoding="utf-8") as stream:
            if lines and not lines[-1].endswith("\n"):
                stream.write("\n")
            stream.write(f"youtube {video_id}\n")
        if path == CHECK_FILE:
            with _lock:
                global _cached_mtime
                _cached_mtime = None
                _cached_ids.add(video_id)
        return True
    except OSError:
        app.logger.exception("Could not add %s to archive %s", video_id, path)
        return False


def _cleanup_report() -> dict:
    """Return removable media candidates without changing any files."""
    entries: list[dict] = []
    audio_checks: list[dict] = []
    location_checks: list[dict] = []
    recoverability_cache: dict[tuple[str, str, str], dict] = {}

    def media_url(path: Path) -> str | None:
        for root, prefix in ((MERGED_DIR, "/media"), (LEGACY_MERGED_DIR, "/media-legacy"), (FALLBACK_DIR, "/media-fallback")):
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            return prefix + "/" + "/".join(quote(part, safe="") for part in relative.parts)
        return None

    def media_state(directory: Path) -> tuple[bool, bool]:
        video = audio = False
        for path in directory.iterdir() if directory.is_dir() else []:
            if not path.is_file() or _SIDECAR_RE.search(path.name):
                continue
            if path.suffix.lower() in {".mkv", ".mp4", ".webm"}:
                video = True
            if path.suffix.lower() in {".m4a", ".opus", ".mp3"}:
                audio = True
        return video, audio

    primary: dict[str, Path] = {}
    for channel_dir in MERGED_DIR.iterdir() if MERGED_DIR.is_dir() else []:
        for video_dir in channel_dir.iterdir() if channel_dir.is_dir() else []:
            if video_dir.is_dir() and not _is_playlist_dir(video_dir) and all(media_state(video_dir)):
                primary[video_dir.name] = video_dir

    location_checks.extend(_legacy_move_checks())

    def add(path: Path, category: str, reason: str) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        replacement_dir = primary.get(path.parent.name) or path.parent
        replacements = []
        for pattern in ("*.out.mkv", "*.out.m4a"):
            for replacement in sorted(replacement_dir.glob(pattern)):
                replacements.append({"name": replacement.name, "path": str(replacement), "url": media_url(replacement), "bytes": replacement.stat().st_size, "location": "strict data/media-strict/" if replacement.is_relative_to(MERGED_DIR) else "legacy data/media-legacy/"})
        entries.append({"video_id": path.parent.name, "path": str(path), "url": media_url(path), "category": category, "reason": reason, "bytes": size, "primary_files": replacements})

    def check_audio(path: Path) -> None:
        ffprobe = shutil.which("ffprobe")
        result = {"video_id": path.parent.name, "path": str(path), "url": media_url(path), "status": "unknown", "reason": "ffprobe is unavailable"}
        if ffprobe:
            try:
                probe = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "a:0",
                     "-show_entries", "stream=codec_name,duration", "-of", "json", str(path)],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                streams = json.loads(probe.stdout or "{}").get("streams") or []
                if probe.returncode == 0 and streams:
                    result = {"video_id": path.parent.name, "path": str(path), "url": media_url(path), "status": "yes",
                              "reason": f"{streams[0].get('codec_name', 'audio')} stream is readable"}
                else:
                    result = {"video_id": path.parent.name, "path": str(path), "url": media_url(path), "status": "no",
                              "reason": (probe.stderr or "no readable audio stream").strip().splitlines()[-1][:240]}
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                result["reason"] = str(exc)[:240]
        audio_checks.append(result)

    def recoverability(path: Path, video_dir: Path) -> dict:
        """Check whether a raw stream can be remuxed from the final MKV."""
        ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
        mkv = next(iter(sorted(video_dir.glob("*.out.mkv"))), None)
        if not ffprobe or not ffmpeg or not mkv:
            return {"status": "unknown", "reason": "ffprobe/ffmpeg or final MKV is unavailable"}
        match = re.search(r"\.out\.f(\d+)\.", path.name)
        format_id = match.group(1) if match else ""
        stream_type = "audio" if path.suffix.lower() == ".m4a" or format_id in {"140", "251"} else "video"
        cache_key = (str(video_dir), stream_type, path.suffix.lower())
        if cache_key in recoverability_cache:
            return recoverability_cache[cache_key]
        try:
            selector = "a:0" if stream_type == "audio" else "v:0"
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", selector,
                 "-show_entries", "stream=codec_name", "-of", "json", str(mkv)],
                capture_output=True, text=True, timeout=15, check=False,
            )
            streams = json.loads(probe.stdout or "{}").get("streams") or []
            if probe.returncode or not streams:
                result = {"status": "no", "reason": f"final MKV has no {stream_type} stream"}
                recoverability_cache[cache_key] = result
                return result
            output_format = {".mp4": "mp4", ".webm": "webm", ".m4a": "ipod"}.get(path.suffix.lower())
            if not output_format:
                result = {"status": "unknown", "reason": "unsupported target container"}
                recoverability_cache[cache_key] = result
                return result
            mux = subprocess.run(
                [ffmpeg, "-v", "error", "-t", "0.1", "-i", str(mkv), "-map", f"0:{'a' if stream_type == 'audio' else 'v'}:0",
                 "-c", "copy", "-f", output_format, "-y", "/dev/null"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if mux.returncode:
                detail = (mux.stderr or "container remux rejected").strip().splitlines()[-1]
                result = {"status": "no", "reason": detail[:240]}
                recoverability_cache[cache_key] = result
                return result
            result = {"status": "yes", "reason": f"{streams[0].get('codec_name', 'matching')} stream can be remuxed"}
            recoverability_cache[cache_key] = result
            return result
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result = {"status": "unknown", "reason": str(exc)[:240]}
            recoverability_cache[cache_key] = result
            return result

    for channel_dir in FALLBACK_DIR.iterdir() if FALLBACK_DIR.is_dir() else []:
        for video_dir in channel_dir.iterdir() if channel_dir.is_dir() else []:
            if video_dir.is_dir() and not _is_playlist_dir(video_dir) and video_dir.name in primary:
                for path in video_dir.rglob("*"):
                    if path.is_file():
                        add(path, "fallback duplicate", "complete strict copy exists under data/media-strict/")

    for root in (MERGED_DIR, LEGACY_MERGED_DIR):
        for path in root.rglob("*") if root.is_dir() else []:
            if not path.is_file() or not path.parent.is_dir():
                continue
            video_dir = path.parent
            if _is_playlist_dir(video_dir):
                continue
            video, audio = media_state(video_dir)
            if path.suffix.lower() in {".m4a", ".opus", ".mp3", ".webm"} and not _SIDECAR_RE.search(path.name):
                check_audio(path)
            if not (video and audio):
                continue
            if _SIDECAR_RE.search(path.name):
                add(path, "raw format sidecar", "final playable video and audio files exist")
                entries[-1]["recoverability"] = recoverability(path, video_dir)
            elif re.search(r"\.out\.webm$", path.name, re.IGNORECASE):
                add(path, "optional clean duplicate", "final MKV and M4A files exist")

    entries.sort(key=lambda item: (-item["bytes"], item["path"]))
    audio_checks.sort(key=lambda item: item["path"])
    safe_paths = {item["path"] for item in entries}
    inventory: dict[str, list[dict]] = {}
    for root in (MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR):
        for path in root.rglob("*") if root.is_dir() else []:
            if not path.is_file():
                continue
            if _is_playlist_dir(path.parent):
                continue
            item = {"path": str(path), "url": media_url(path),
                    "status": "delete" if str(path) in safe_paths else "keep",
                    "bytes": path.stat().st_size}
            inventory.setdefault(path.parent.name, []).append(item)
    for files in inventory.values():
        files.sort(key=lambda item: item["path"])
    expected_state: dict[str, dict[str, bool]] = {}
    for root in (MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR):
        for channel_dir in root.iterdir() if root.is_dir() else []:
            for video_dir in channel_dir.iterdir() if channel_dir.is_dir() else []:
                if not video_dir.is_dir() or _is_playlist_dir(video_dir):
                    continue
                checks = {
                    "playable video (.out.mkv)": bool(list(video_dir.glob("*.out.mkv"))),
                    "playable audio (.out.m4a)": bool(list(video_dir.glob("*.out.m4a"))),
                    "metadata (.out.info.json)": bool(list(video_dir.glob("*.out.info.json"))),
                    "formats (.out.formats.json)": bool(list(video_dir.glob("*.out.formats.json"))),
                    "description (.out.description)": bool(list(video_dir.glob("*.out.description"))),
                    "thumbnail (.out.webp/.png/.jpg)": bool(list(video_dir.glob("*.out.webp")) or list(video_dir.glob("*.out.png")) or list(video_dir.glob("*.out.jpg"))),
                }
                state = expected_state.setdefault(video_dir.name, {label: False for label in checks})
                for label, present in checks.items():
                    state[label] = state.get(label, False) or present
    expected = {video_id: sorted(label for label, present in state.items() if not present)
                for video_id, state in expected_state.items()}
    try:
        full_checks = json.loads(_VERIFY_CACHE.read_text()) if _VERIFY_CACHE.is_file() else {}
    except (OSError, ValueError):
        full_checks = {}
    return {"dry_run": True, "entries": entries, "count": len(entries),
            "bytes": sum(item["bytes"] for item in entries), "audio_checks": audio_checks,
            "location_checks": location_checks, "inventory": inventory, "missing_expected": expected,
            "full_checks": full_checks}


def _scan_tags() -> dict:
    videos = _scan_library_cached()
    tags: dict[str, list[dict]] = {}
    for video in videos:
        video_dir = next(
            (root / video["channel_id"] / video["video_id"]
             for root in (MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR)
             if (root / video["channel_id"] / video["video_id"]).is_dir()),
            MERGED_DIR / video["channel_id"] / video["video_id"],
        )
        info, _error = _load_info_json(video_dir)
        for tag in info.get("tags") or [] if info else []:
            tag = str(tag).strip()
            if not tag:
                continue
            tags.setdefault(tag, []).append(_slim_video(video))

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
    response = make_response(render_template("catalog.html"))
    response.headers["Content-Language"] = "en"
    return response


@app.get("/library-export")
def library_export_page():
    return render_template("library_export.html")


@app.post("/api/media/catalog/refresh")
def api_media_catalog_refresh():
    """Rebuild the filesystem-backed catalog and remove stale rows."""
    try:
        count = catalog.refresh([MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR], CATALOG_DB, CHECK_FILE)
    except Exception:
        app.logger.exception("Catalog refresh failed")
        return jsonify(ok=False, error="catalog refresh failed"), 503
    return jsonify(ok=True, count=count)


@app.get("/cleanup")
def cleanup_page():
    return render_template("cleanup.html")


@app.get("/api/media/cleanup-report")
def api_media_cleanup_report():
    return jsonify(_get_cleanup_report())


def _get_cleanup_report() -> dict:
    global _cleanup_report_cache
    now = time.time()
    if _cleanup_report_cache and now - _cleanup_report_cache[0] < 15:
        return _cleanup_report_cache[1]
    report = _cleanup_report()
    _cleanup_report_cache = (now, report)
    return report


def _cleanup_candidate_paths() -> set[Path]:
    return {Path(item["path"]).resolve() for item in _get_cleanup_report()["entries"]}


def _legacy_move_checks() -> list[dict]:
    checks = []
    for channel_dir in LEGACY_MERGED_DIR.iterdir() if LEGACY_MERGED_DIR.is_dir() else []:
        for video_dir in channel_dir.iterdir() if channel_dir.is_dir() else []:
            if not video_dir.is_dir() or _is_playlist_dir(video_dir):
                continue
            target = MERGED_DIR / channel_dir.name / video_dir.name
            item = {"video_id": video_dir.name, "status": "conflict" if target.exists() else "movable",
                    "source": str(video_dir), "target": str(target)}
            if target.exists():
                matches, different, unique = [], [], []
                for source_file in video_dir.rglob("*"):
                    if not source_file.is_file():
                        continue
                    target_file = target / source_file.relative_to(video_dir)
                    if not target_file.is_file():
                        unique.append(str(source_file)); continue
                    try:
                        source_hash = hashlib.md5(source_file.read_bytes()).hexdigest()
                        target_hash = hashlib.md5(target_file.read_bytes()).hexdigest()
                    except OSError:
                        different.append(str(source_file)); continue
                    if source_hash == target_hash:
                        matches.append(str(source_file))
                    else:
                        different.append({"source": str(source_file), "target": str(target_file), "name": source_file.name})
                item["comparison"] = {"matches": matches, "different": different, "legacy_only": unique,
                                       "empty": not any(path.is_file() for path in video_dir.rglob("*"))}
            else:
                item["empty"] = not any(path.is_file() for path in video_dir.rglob("*"))
            checks.append(item)
    return checks


def _start_cleanup_task(worker) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _cleanup_tasks[task_id] = {"id": task_id, "status": "running", "phase": "starting", "processed": 0, "total": 0}

    def run():
        try:
            result = worker(task_id) or {}
            with _lock:
                _cleanup_tasks[task_id].update(result, status="completed", phase="done")
            global _cleanup_report_cache
            _cleanup_report_cache = None
        except Exception as exc:
            app.logger.exception("Cleanup task failed: %s", task_id)
            with _lock:
                _cleanup_tasks[task_id].update(status="failed", phase="error", error=str(exc))
    threading.Thread(target=run, name=f"cleanup-{task_id}", daemon=True).start()
    return task_id


@app.get("/api/media/cleanup/tasks/<string:task_id>")
def api_media_cleanup_task(task_id: str):
    with _lock:
        task = dict(_cleanup_tasks.get(task_id) or {})
    return jsonify(task) if task else (jsonify(ok=False, error="cleanup task not found"), 404)


@app.post("/api/media/cleanup/verify")
def api_media_cleanup_verify():
    def worker(task_id):
        report = _get_cleanup_report()
        paths = [Path(x["path"]) for files in report.get("inventory", {}).values() for x in files
                 if Path(x["path"]).suffix.lower() in {".mkv", ".mp4", ".webm", ".m4a", ".opus", ".mp3"}]
        try:
            cache = json.loads(_VERIFY_CACHE.read_text()) if _VERIFY_CACHE.is_file() else {}
        except (OSError, ValueError):
            cache = {}
        results = {}
        with _lock: _cleanup_tasks[task_id].update(phase="full verification", total=len(paths))
        for index, path in enumerate(paths, 1):
            digest = hashlib.md5()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
            key = str(path)
            old = cache.get(key)
            if old and old.get("md5") == digest.hexdigest():
                result = old
            else:
                probe = shutil.which("ffmpeg")
                if not probe: result = {"md5": digest.hexdigest(), "status": "unknown", "reason": "ffmpeg unavailable"}
                else:
                    check = subprocess.run([probe, "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True, timeout=3600, check=False)
                    result = {"md5": digest.hexdigest(), "status": "yes" if check.returncode == 0 else "no", "reason": "full decode passed" if check.returncode == 0 else (check.stderr or "full decode failed").strip().splitlines()[-1][:240]}
                cache[key] = result
            results[key] = {**result, "video_id": path.parent.name, "path": key, "url": media_url(path)}
            with _lock: _cleanup_tasks[task_id]["processed"] = index
        _VERIFY_CACHE.parent.mkdir(parents=True, exist_ok=True); _VERIFY_CACHE.write_text(json.dumps(cache, indent=2))
        return {"checks": list(results.values()), "processed": len(paths), "total": len(paths)}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


@app.post("/api/media/cleanup/retry-missing")
def api_media_cleanup_retry_missing():
    report = _get_cleanup_report()
    video_ids = set(report.get("inventory", {}))
    missing = []
    skipped = []
    for video_id in video_ids:
        video = _resolve_media_by_video_id(video_id) or {}
        files = video.get("files") or {}
        if files.get("video") and files.get("audio"):
            continue
        if _permanent_failure(video_id):
            skipped.append(video_id)
        else:
            missing.append(video_id)
    queue_status = "pending" if catalog.setting(CATALOG_DB, "default_download_mode", "immediate") == "immediate" else "paused"
    queued = []
    for video_id in sorted(missing):
        item_id, already = _enqueue_target(video_id, "video", status=queue_status)
        if not already:
            queued.append(item_id)
    if queued:
        _queue_wakeup.set()
    return jsonify(ok=True, found=len(missing), queued=len(queued), skipped_permanent=len(skipped))


@app.post("/api/media/cleanup/delete")
def api_media_cleanup_delete():
    path = Path(str((request.get_json(silent=True) or {}).get("path") or "")).resolve()
    if path not in _cleanup_candidate_paths():
        return jsonify(ok=False, error="file is not a current cleanup candidate"), 400
    def worker(task_id):
        with _lock: _cleanup_tasks[task_id].update(phase="deleting", total=1)
        path.unlink()
        return {"deleted": 1, "processed": 1, "total": 1, "path": str(path)}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


@app.post("/api/media/cleanup/delete-all")
def api_media_cleanup_delete_all():
    def worker(task_id):
        paths = list(_cleanup_candidate_paths())
        with _lock: _cleanup_tasks[task_id].update(phase="deleting", total=len(paths))
        removed = bytes_removed = 0
        for index, path in enumerate(paths, 1):
            try:
                size = path.stat().st_size; path.unlink(); removed += 1; bytes_removed += size
            except OSError: pass
            with _lock: _cleanup_tasks[task_id]["processed"] = index
        return {"removed": removed, "bytes": bytes_removed, "total": len(paths), "processed": len(paths)}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


@app.post("/api/media/cleanup/move-legacy")
def api_media_cleanup_move_legacy():
    video_id = str((request.get_json(silent=True) or {}).get("video_id") or "").strip()
    check = next((item for item in _legacy_move_checks() if item["video_id"] == video_id), None)
    if not check or check["status"] != "movable":
        return jsonify(ok=False, error="legacy folder is not safely movable"), 400
    source, target = Path(check["source"]).resolve(), Path(check["target"]).resolve()
    if not source.is_relative_to(LEGACY_MERGED_DIR) or target.exists():
        return jsonify(ok=False, error="invalid or conflicting move target"), 400
    def worker(task_id):
        total = sum(1 for item in source.rglob("*") if item.is_file())
        with _lock: _cleanup_tasks[task_id].update(phase="moving", total=total)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        with _lock: _cleanup_tasks[task_id]["processed"] = total
        return {"moved": 1, "video_id": video_id, "source": str(source), "target": str(target), "total": total, "processed": total}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


@app.post("/api/media/cleanup/move-legacy-all")
def api_media_cleanup_move_legacy_all():
    movable = [item for item in _legacy_move_checks() if item["status"] == "movable"]
    def worker(task_id):
        with _lock: _cleanup_tasks[task_id].update(phase="moving legacy folders", total=len(movable))
        moved = 0
        for index, item in enumerate(movable, 1):
            source, target = Path(item["source"]).resolve(), Path(item["target"]).resolve()
            with _lock: _cleanup_tasks[task_id]["phase"] = f"moving {item['video_id']}"
            if source.is_relative_to(LEGACY_MERGED_DIR) and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target)); moved += 1
            with _lock: _cleanup_tasks[task_id]["processed"] = index
        return {"moved": moved, "skipped": len(movable) - moved, "total": len(movable), "processed": len(movable)}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


@app.post("/api/media/cleanup/delete-legacy-matches")
def api_media_cleanup_delete_legacy_matches():
    video_id = str((request.get_json(silent=True) or {}).get("video_id") or "").strip()
    check = next((item for item in _legacy_move_checks() if item["video_id"] == video_id), None)
    if not check or check["status"] != "conflict":
        return jsonify(ok=False, error="no legacy conflict found"), 400
    paths = [Path(path).resolve() for path in check.get("comparison", {}).get("matches", [])]
    def worker(task_id):
        with _lock: _cleanup_tasks[task_id].update(phase="deleting matching legacy files", total=len(paths))
        removed = 0
        for index, path in enumerate(paths, 1):
            if path.is_relative_to(LEGACY_MERGED_DIR) and path.is_file():
                path.unlink(); removed += 1
            with _lock: _cleanup_tasks[task_id]["processed"] = index
        return {"deleted": removed, "processed": len(paths), "total": len(paths)}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


@app.post("/api/media/cleanup/delete-legacy-different")
def api_media_cleanup_delete_legacy_different():
    video_id = str((request.get_json(silent=True) or {}).get("video_id") or "").strip()
    check = next((item for item in _legacy_move_checks() if item["video_id"] == video_id), None)
    if not check or check["status"] != "conflict":
        return jsonify(ok=False, error="no legacy conflict found"), 400
    paths = [Path(item["source"]).resolve() for item in check.get("comparison", {}).get("different", [])]
    def worker(task_id):
        with _lock: _cleanup_tasks[task_id].update(phase="deleting different legacy files", total=len(paths))
        removed = 0
        for index, path in enumerate(paths, 1):
            if path.is_relative_to(LEGACY_MERGED_DIR) and path.is_file(): path.unlink(); removed += 1
            with _lock: _cleanup_tasks[task_id]["processed"] = index
        return {"deleted": removed, "processed": len(paths), "total": len(paths)}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


@app.post("/api/media/cleanup/move-legacy-only")
def api_media_cleanup_move_legacy_only():
    video_id = str((request.get_json(silent=True) or {}).get("video_id") or "").strip()
    check = next((item for item in _legacy_move_checks() if item["video_id"] == video_id), None)
    if not check or check["status"] != "conflict":
        return jsonify(ok=False, error="no legacy conflict found"), 400
    source_root, target_root = Path(check["source"]).resolve(), Path(check["target"]).resolve()
    paths = [Path(path).resolve() for path in check.get("comparison", {}).get("legacy_only", [])]
    def worker(task_id):
        with _lock: _cleanup_tasks[task_id].update(phase="moving legacy-only files", total=len(paths))
        moved = 0
        for index, path in enumerate(paths, 1):
            target = target_root / path.relative_to(source_root)
            if path.is_relative_to(LEGACY_MERGED_DIR) and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(path), str(target)); moved += 1
            with _lock: _cleanup_tasks[task_id]["processed"] = index
        return {"moved": moved, "processed": len(paths), "total": len(paths)}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


def _empty_legacy_dirs(source: Path) -> int:
    """Remove only empty directories in the legacy tree, never files."""
    removed = 0
    directories = sorted((path for path in source.rglob("*") if path.is_dir()),
                         key=lambda path: len(path.parts), reverse=True)
    directories.append(source)
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            continue
        removed += 1
    parent = source.parent
    if parent.is_relative_to(LEGACY_MERGED_DIR):
        try:
            parent.rmdir()
        except OSError:
            pass
        else:
            removed += 1
    return removed


@app.post("/api/media/cleanup/delete-empty-legacy")
def api_media_cleanup_delete_empty_legacy():
    video_id = str((request.get_json(silent=True) or {}).get("video_id") or "").strip()
    check = next((item for item in _legacy_move_checks() if item["video_id"] == video_id), None)
    source = Path(check["source"]).resolve() if check else None
    if not source or not source.is_relative_to(LEGACY_MERGED_DIR) or not source.is_dir():
        return jsonify(ok=False, error="legacy folder not found"), 400
    if any(path.is_file() for path in source.rglob("*")):
        return jsonify(ok=False, error="legacy folder is not empty"), 400

    def worker(task_id):
        with _lock: _cleanup_tasks[task_id].update(phase="deleting empty legacy directories", total=1)
        removed = _empty_legacy_dirs(source)
        with _lock: _cleanup_tasks[task_id]["processed"] = 1
        return {"deleted_dirs": removed, "processed": 1, "total": 1}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


@app.post("/api/media/cleanup/delete-empty-legacy-all")
def api_media_cleanup_delete_empty_legacy_all():
    sources = []
    for check in _legacy_move_checks():
        source = Path(check["source"]).resolve()
        if source.is_relative_to(LEGACY_MERGED_DIR) and source.is_dir() and not any(path.is_file() for path in source.rglob("*")):
            sources.append(source)

    def worker(task_id):
        with _lock: _cleanup_tasks[task_id].update(phase="deleting empty legacy directories", total=len(sources))
        removed = 0
        for index, source in enumerate(sources, 1):
            if source.is_dir() and not any(path.is_file() for path in source.rglob("*")):
                removed += _empty_legacy_dirs(source)
            with _lock: _cleanup_tasks[task_id]["processed"] = index
        return {"deleted_dirs": removed, "processed": len(sources), "total": len(sources)}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


@app.post("/api/media/cleanup/delete-legacy-different-all")
def api_media_cleanup_delete_legacy_different_all():
    paths = [Path(item["source"]).resolve()
             for check in _legacy_move_checks() if check["status"] == "conflict"
             for item in check.get("comparison", {}).get("different", [])]

    def worker(task_id):
        with _lock: _cleanup_tasks[task_id].update(phase="deleting different legacy files", total=len(paths))
        removed = 0
        for index, path in enumerate(paths, 1):
            if path.is_relative_to(LEGACY_MERGED_DIR) and path.is_file():
                path.unlink(); removed += 1
            with _lock: _cleanup_tasks[task_id]["processed"] = index
        return {"deleted": removed, "processed": len(paths), "total": len(paths)}
    return jsonify(ok=True, task_id=_start_cleanup_task(worker)), 202


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


@app.get("/library-tests")
def library_tests_page():
    return render_template("library_tests.html")


@app.get("/api-docs")
def api_docs_page():
    endpoints = [
        ("GET", "/health", "Health and active download counts"),
        ("GET", "/api/youtube/<video_id>", "Check archive status"),
        ("POST", "/api/youtube/get/<video_id>", "Add or start a video download"),
        ("POST", "/api/youtube/retry/<video_id>", "Retry a failed video"),
        ("GET", "/api/youtube/status/<video_id>", "Video download progress"),
        ("POST", "/api/youtube/playlist/get/<playlist_id>", "Add or start a playlist download"),
        ("POST", "/api/youtube/playlist/prepare/<playlist_id>", "Save playlist name and members without downloading"),
        ("GET", "/api/youtube/playlist/status/<playlist_id>", "Playlist download progress"),
        ("GET", "/api/downloads/status", "Active and recent download status"),
        ("GET", "/api/queue", "Persistent queue items and default mode"),
        ("POST", "/api/queue", "Add a video or playlist to the queue"),
        ("POST", "/api/queue/<item_id>/start", "Start a paused queue item"),
        ("POST", "/api/queue/<item_id>/pause", "Pause a running queue item after its current download boundary"),
        ("POST", "/api/queue/<item_id>/stop", "Stop a running queue item and leave it paused"),
        ("POST", "/api/queue/<item_id>/cancel", "Cancel a pending or running queue item; running items stop first"),
        ("POST", "/api/queue/clear", "Remove pending and paused queue items without interrupting active downloads"),
        ("GET/POST", "/api/settings", "Read or update queue defaults"),
        ("GET", "/api/media/library", "List local media"),
        ("GET", "/api/media/library/files", "List links for every local library file"),
        ("GET", "/api/media/library/files.txt", "Export every local library file link as text"),
        ("GET", "/api/media/library/youtube.txt", "Export all video and playlist YouTube links"),
        ("GET", "/api/media/library/ids.txt", "Export all video and playlist IDs"),
        ("GET", "/api/media/resolve/<video_id>", "Resolve playable local media"),
        ("GET", "/api/media/details/<channel_id>/<video_id>", "Return files and metadata"),
        ("GET", "/api/media/playlists", "List indexed playlists"),
        ("GET", "/api/media/playlists/<playlist_id>", "Return playlist members"),
        ("POST", "/api/media/playlists/<playlist_id>/add", "Add an indexed video to a saved playlist"),
        ("POST", "/api/media/playlists/snapshot", "Save browser-captured playlist members"),
        ("DELETE", "/api/media/playlists/<playlist_id>", "Remove a local snapshot descriptor without deleting media"),
        ("GET", "/api/media/playlists/<playlist_id>.m3u?mode=video|audio", "Export a VLC playlist"),
        ("GET", "/api/media/catalog", "Paginated catalog data"),
        ("GET", "/api/media/cleanup-report", "Dry-run cleanup candidates and disk savings"),
        ("GET", "/api/media/tags", "Tag counts and grouped videos"),
        ("GET", "/api/media/failures", "Latest unresolved failures"),
        ("GET", "/api/media/download-history", "Persistent download history"),
        ("GET", "/api/media/wordcloud", "Description word frequencies"),
        ("GET", "/api/system/status", "Disk, directory, and cookie diagnostics"),
        ("GET", "/api/system/cache-status", "Cache coverage and refresh state"),
        ("GET", "/api/system/startup-history", "Persisted cold/warm startup timings"),
    ]
    return render_template("api-docs.html", endpoints=endpoints)


@app.get("/sitemap")
def sitemap_page():
    groups = [
        ("Library", [("Media library", "/"), ("Playlists", "/playlists"), ("Video detail", "/video/dQw4w9WgXcQ"), ("Catalog", "/catalog"), ("Export files", "/library-export")]),
        ("Downloads", [("Downloads status", "/downloads"), ("Persistent queue", "/queue"), ("Downloaded history", "/downloaded")]),
        ("Tools", [("Tools hub", "/tools"), ("Library search/sort tests", "/library-tests"), ("API documentation", "/api-docs"), ("System status", "/status"), ("Tags", "/tags"), ("Word cloud", "/wordcloud"), ("Tag cloud", "/tagcloud")]),
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
def api_media_catalog():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    except ValueError:
        return jsonify(error="page and per_page must be integers"), 400
    sort = request.args.get("sort", "date-desc")
    order = "upload_date ASC" if sort == "date-asc" else "artist COLLATE NOCASE ASC, title COLLATE NOCASE ASC" if sort == "artist" else "title COLLATE NOCASE ASC" if sort == "title" else "upload_date DESC"
    try:
        with sqlite3.connect(CATALOG_DB, timeout=10) as db:
            # The initial filesystem scan runs in a background thread. Ensure
            # the readable catalog schema exists even while that scan is busy.
            db.executescript(catalog.SCHEMA)
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
        video_id, source_root, channel_id = row[0], row[1], row[2]
        files = json.loads(row[9] or "{}")
        formats = json.loads(row[10] or "[]")
        # A download may finish in data/media-strict/ after the catalog's last scan. In
        # that case, rebuild this row from the live directory so thumbnail and
        # media links do not remain pointed at a stale legacy location.
        live_roots = [
            (MERGED_DIR, "merged"),
            (LEGACY_MERGED_DIR, "legacy"),
            (FALLBACK_DIR, "bestfallback"),
        ]
        live_dir = next((root / channel_id / video_id for root, name in live_roots
                         if (root / channel_id / video_id).is_dir() and not _is_playlist_dir(root / channel_id / video_id)), None)
        if live_dir:
            live_source = next(name for root, name in live_roots if live_dir.is_relative_to(root))
            live_info = catalog._info(live_dir)
            source_root, files, formats = live_source, catalog._files(live_dir, channel_id, video_id, live_source), catalog._formats(live_dir)
            if live_info:
                title = live_info.get("title") or row[3]
                artist = live_info.get("artist") or live_info.get("uploader") or row[4]
                album = live_info.get("album") or live_info.get("playlist_title") or row[5]
                uploader = live_info.get("uploader") or live_info.get("channel") or row[6]
                upload_date = live_info.get("upload_date") or row[7]
                duration = live_info.get("duration") or row[8]
            else:
                title, artist, album, uploader, upload_date, duration = row[3:9]
        else:
            title, artist, album, uploader, upload_date, duration = row[3:9]
        items.append({"video_id": video_id, "source_root": source_root, "channel_id": channel_id, "title": title, "display_title": clean_display_title(title, artist or uploader), "artist": artist, "album": album, "uploader": uploader, "upload_date": upload_date, "human_date": human_date(upload_date), "duration": duration, "files": files, "formats": formats, "failure_reason": row[11] or ""})
    return jsonify(items=items, page=page, per_page=per_page, total=total, pages=(total + per_page - 1) // per_page)


def _catalog_library_or_scan(query: str | None = None) -> list[dict]:
    """Return slim cards from the catalog, falling back to a live scan.

    The catalog is rebuildable from disk via ``catalog.refresh``; the scan
    fallback covers the first-startup window before the background refresh
    finishes and any catalog read error.
    """
    try:
        items, _total = catalog.library_cards(CATALOG_DB, q=query)
        if items:
            return items
    except (sqlite3.Error, OSError, ValueError):
        pass
    cards = [_slim_video(video) for video in _scan_library_cached()]
    needle = (query or "").strip().lower()
    if needle:
        cards = [card for card in cards
                 if needle in str(card.get("title") or "").lower()
                 or needle in str(card.get("video_id") or "").lower()
                 or needle in str(card.get("channel_id") or "").lower()
                 or needle in str(card.get("artist") or "").lower()
                 or needle in str(card.get("uploader") or "").lower()
                 or needle in str(card.get("track") or "").lower()]
    return cards


@app.get("/api/media/library")
def api_media_library():
    sort = request.args.get("sort", "date-desc")
    query = (request.args.get("q") or "").strip() or None
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    except ValueError:
        return jsonify(error="page and per_page must be integers"), 400
    # The library UI has no separate pagination control: a search or sort
    # request must return the complete matching card set. Explicit ``page``
    # requests remain available for API consumers.
    paginated = request.args.get("page") is not None
    if not _catalog_ready.is_set():
        return jsonify(videos=[], page=page, per_page=per_page, total=0, pages=0,
                       initializing=True, message="Media catalog is still building")
    try:
        if paginated:
            items, total = catalog.library_cards(
                CATALOG_DB, page=page, per_page=per_page, sort=sort, q=query)
        else:
            items, total = catalog.library_cards(CATALOG_DB, sort=sort, q=query)
        if total:
            if paginated:
                return jsonify(videos=items, page=page, per_page=per_page, total=total,
                               pages=(total + per_page - 1) // per_page)
            return jsonify(videos=items, total=total)
    except (sqlite3.Error, OSError, ValueError):
        pass
    # Catalog empty (first-startup window) or unreadable: fall back to a live
    # scan. A catalog total of 0 with a non-empty scan means unindexed, not
    # "no match", so the scan result wins in that case.
    cards = _catalog_library_or_scan(query)
    if paginated:
        total = len(cards)
        start = (page - 1) * per_page
        return jsonify(videos=cards[start:start + per_page], page=page, per_page=per_page,
                       total=total, pages=(total + per_page - 1) // per_page)
    return jsonify(videos=cards, total=len(cards))


@app.get("/api/media/library/files")
def api_media_library_files():
    cached = _read_derived_cache(_EXPORTS_CACHE)
    if isinstance(cached, dict) and isinstance(cached.get("files"), list):
        return jsonify(files=cached["files"], count=len(cached["files"]))
    try:
        files = catalog.library_files(CATALOG_DB)
        if files:
            _write_derived_cache(_EXPORTS_CACHE, {"files": files})
            return jsonify(files=files, count=len(files))
    except (sqlite3.Error, OSError, ValueError):
        pass
    files = []
    for video in _scan_library_cached():
        for item in video.get("details", {}).get("files", []):
            files.append({"video_id": video["video_id"], "title": video["title"], **item})
    _write_derived_cache(_EXPORTS_CACHE, {"files": files})
    return jsonify(files=files, count=len(files))


@app.get("/api/media/library/files.txt")
def api_media_library_files_text():
    cached = _read_derived_cache(_EXPORTS_CACHE)
    files = cached.get("files") if isinstance(cached, dict) else None
    if not isinstance(files, list):
        response = api_media_library_files()
        files = response.get_json().get("files", [])
    lines = [f"{item['video_id']}\t{item['title']}\t{item['name']}\t{request.host_url.rstrip('/')}{item['url']}"
             for item in files if item.get("url")]
    return Response("\n".join(lines) + ("\n" if lines else ""), mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=dihi-library-files.txt"})


def _library_youtube_exports() -> tuple[list[str], list[str]]:
    cached = _read_derived_cache(_EXPORTS_CACHE)
    cached_payload = cached if isinstance(cached, dict) else {}
    if isinstance(cached, dict) and isinstance(cached.get("youtube"), list) and isinstance(cached.get("ids"), list):
        return cached["youtube"], cached["ids"]
    try:
        links, ids = catalog.library_exports(CATALOG_DB)
        if links or ids:
            _write_derived_cache(_EXPORTS_CACHE, {"files": cached_payload.get("files", []), "youtube": links, "ids": ids})
            return links, ids
    except (sqlite3.Error, OSError, ValueError):
        pass
    videos, playlists, video_ids, playlist_ids = [], [], [], []
    seen_videos, seen_playlists = set(), set()
    for video in _scan_library_cached():
        video_id = str(video.get("video_id") or "").strip()
        info = video.get("details", {}).get("metadata", {}).get("info_json", {})
        if video_id and video_id not in seen_videos:
            seen_videos.add(video_id); video_ids.append(video_id)
            videos.append(str(info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"))
        playlist_id = str(info.get("playlist_id") or "").strip()
        if playlist_id and playlist_id not in seen_playlists:
            seen_playlists.add(playlist_id); playlist_ids.append(playlist_id)
            playlists.append(str(info.get("playlist_webpage_url") or f"https://www.youtube.com/playlist?list={playlist_id}"))
    try:
        for playlist in catalog.playlists(CATALOG_DB):
            playlist_id = str(playlist.get("playlist_id") or "").strip()
            if playlist_id and playlist_id not in seen_playlists:
                seen_playlists.add(playlist_id); playlist_ids.append(playlist_id)
                playlists.append(str(playlist.get("webpage_url") or f"https://www.youtube.com/playlist?list={playlist_id}"))
    except sqlite3.Error:
        pass
    links, ids = videos + playlists, [*(f"video {value}" for value in video_ids), *(f"playlist {value}" for value in playlist_ids)]
    _write_derived_cache(_EXPORTS_CACHE, {"files": cached_payload.get("files", []), "youtube": links, "ids": ids})
    return links, ids


@app.get("/api/media/library/youtube.txt")
def api_media_library_youtube_text():
    links, _ids = _library_youtube_exports()
    return Response("\n".join(links) + ("\n" if links else ""), mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=dihi-youtube-links.txt"})


@app.get("/api/media/library/ids.txt")
def api_media_library_ids_text():
    _links, ids = _library_youtube_exports()
    return Response("\n".join(ids) + ("\n" if ids else ""), mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=dihi-youtube-and-playlist-ids.txt"})


@app.get("/api/media/playlists")
def api_media_playlists():
    descriptors = _descriptor_playlist_cards()
    if descriptors:
        return jsonify(playlists=descriptors)
    try:
        return jsonify(playlists=catalog.playlists(CATALOG_DB))
    except (sqlite3.Error, OSError, ValueError):
        return jsonify(playlists=_descriptor_playlist_cards())


@app.post("/api/media/playlists/snapshot")
def api_media_playlist_snapshot():
    """Save browser-captured members of an autogenerated playlist as a local snapshot."""
    payload = request.get_json(silent=True) or {}
    source_id = str(payload.get("source_playlist_id") or "").strip()
    if not PLAYLIST_ID_RE.match(source_id):
        return jsonify(ok=False, error="invalid source playlist id"), 400
    raw_members = payload.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        return jsonify(ok=False, error="snapshot must contain at least one video"), 400
    members, seen = [], set()
    for index, raw in enumerate(raw_members, 1):
        if not isinstance(raw, dict):
            continue
        video_id = _normalize_id(str(raw.get("video_id") or raw.get("id") or ""))
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        members.append({
            "video_id": video_id,
            "playlist_index": raw.get("playlist_index") or index,
            "title": str(raw.get("title") or video_id),
            "video_url": str(raw.get("video_url") or f"https://www.youtube.com/watch?v={video_id}"),
        })
    if not members:
        return jsonify(ok=False, error="snapshot contains no valid video IDs"), 400
    now = time.time()
    snapshot_id = f"SNAP_{source_id[:60]}_{int(now * 1000)}"
    title = str(payload.get("title") or source_id).strip()[:150]
    source_url = str(payload.get("webpage_url") or f"https://www.youtube.com/playlist?list={source_id}")
    snapshot_title = f"{title} — snapshot {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}"
    _save_playlist_metadata(snapshot_id, snapshot_title, source_url, members)
    _record_playlist_membership_best_effort(snapshot_id, snapshot_title, source_url, members)
    queued = 0
    if payload.get("queue"):
        start_now = bool(payload.get("start_now", False))
        status = "pending" if start_now else "paused"
        for member in members:
            _enqueue_target(member["video_id"], "video", status=status)
            queued += 1
        if start_now:
            _queue_wakeup.set()
    return jsonify(ok=True, snapshot_id=snapshot_id, title=snapshot_title,
                   source_playlist_id=source_id, members=len(members), queued=queued)


@app.delete("/api/media/playlists/<string:playlist_id>")
def api_media_playlist_delete(playlist_id: str):
    """Remove a local snapshot descriptor; never delete downloaded media."""
    if not playlist_id.startswith("SNAP_") or not PLAYLIST_ID_RE.match(playlist_id):
        return jsonify(ok=False, error="only local snapshots can be removed"), 400
    if not catalog.delete_playlist(CATALOG_DB, playlist_id):
        return jsonify(ok=False, error="snapshot not found"), 404
    descriptor = (PLAYLIST_METADATA_DIR / f"{playlist_id}.info.json").resolve()
    if descriptor.is_relative_to(PLAYLIST_METADATA_DIR.resolve()) and descriptor.is_file():
        descriptor.unlink()
    return jsonify(ok=True, playlist_id=playlist_id, media_deleted=False)


@app.get("/api/media/playlists/<string:playlist_id>")
def api_media_playlist(playlist_id: str):
    if not PLAYLIST_ID_RE.match(playlist_id):
        return jsonify(error="invalid playlist id"), 400
    snapshot = _fast_playlist_snapshot(playlist_id)
    playlist = snapshot[0] if snapshot else None
    snapshot_members = snapshot[1] if snapshot else []
    if playlist is None:
        try:
            with sqlite3.connect(CATALOG_DB, timeout=0.25) as db:
                row = db.execute("SELECT playlist_id, title, webpage_url, updated_at FROM playlists WHERE playlist_id = ?", (playlist_id,)).fetchone()
                rows = db.execute("SELECT video_id, playlist_index, title, video_url FROM playlist_videos WHERE playlist_id = ? ORDER BY playlist_index IS NULL, playlist_index, video_id", (playlist_id,)).fetchall()
            if row:
                playlist = {"playlist_id": row[0], "title": row[1], "webpage_url": row[2], "updated_at": row[3]}
                snapshot_members = [{"video_id": item[0], "playlist_index": item[1], "title": item[2], "video_url": item[3]} for item in rows]
        except (sqlite3.Error, OSError):
            playlist = None
    if not playlist:
        return jsonify(error="playlist not found"), 404
    # Never fall back to one filesystem walk per member here. A large
    # playlist can contain thousands of unavailable IDs, and each walk scans
    # every channel directory on a USB/DrvFs archive. Missing catalog rows
    # remain represented by ``video: null`` and can be refreshed/retried.
    # Large saved descriptors must render while the catalog may still be
    # rebuilding on a USB-backed data root. Returning the saved member rows is
    # preferable to holding the request at the browser's timeout boundary;
    # smaller/indexed playlists still receive playable catalog enrichment.
    if snapshot and len(snapshot_members) > 50:
        # The playlist descriptor is the fast source of truth for large
        # playlists. Reuse the availability map built for the playlist cards
        # so downloaded members are not mislabeled "not local" merely because
        # we skipped expensive per-member metadata enrichment.
        videos_by_id = {
            str(member["video_id"]): {
                "video_id": str(member["video_id"]),
                "title": member.get("title") or member["video_id"],
                "files": {
                    "video": {"kind": "video"},
                    "audio": {"kind": "audio"},
                },
            }
            for member in snapshot_members
            if str(member["video_id"]) in _available_catalog_ids
        }
    else:
        videos_by_id = _fast_catalog_videos({str(member["video_id"]) for member in snapshot_members})
    members = []
    for member in snapshot_members:
        video = videos_by_id.get(member["video_id"])
        members.append({**member, "video": video})
    return jsonify(playlist=playlist, videos=members)


@app.post("/api/media/playlists/<string:playlist_id>/add")
def api_media_playlist_add(playlist_id: str):
    """Add one already-indexed video to a saved playlist without downloading it."""
    if not PLAYLIST_ID_RE.match(playlist_id):
        return jsonify(ok=False, error="invalid playlist id"), 400
    payload = request.get_json(silent=True) or {}
    video_id = _normalize_id(str(payload.get("video_id") or payload.get("id") or ""))
    if not video_id:
        return jsonify(ok=False, error="invalid video id"), 400
    playlist = next((item for item in catalog.playlists(CATALOG_DB)
                     if item["playlist_id"] == playlist_id), None)
    if not playlist:
        return jsonify(ok=False, error="playlist not found"), 404
    members = catalog.playlist_video_ids(CATALOG_DB, playlist_id)
    if any(member["video_id"] == video_id for member in members):
        return jsonify(ok=True, playlist_id=playlist_id, video_id=video_id,
                       already_present=True, members=len(members))
    video = next((item for item in _catalog_library_or_scan()
                  if item.get("video_id") == video_id), None) or {}
    member = {
        "video_id": video_id,
        "playlist_index": len(members) + 1,
        "title": video.get("title") or video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
    }
    members.append(member)
    webpage_url = playlist.get("webpage_url") or f"https://www.youtube.com/playlist?list={playlist_id}"
    _save_playlist_metadata(playlist_id, playlist.get("title") or playlist_id, webpage_url, members)
    _record_playlist_membership_best_effort(playlist_id, playlist.get("title") or playlist_id,
                                            webpage_url, members)
    return jsonify(ok=True, playlist_id=playlist_id, video_id=video_id,
                   already_present=False, members=len(members))


@app.post("/api/media/playlists/<string:playlist_id>/refresh")
def api_media_playlist_refresh(playlist_id: str):
    """Refresh membership only; never removes local media files."""
    if not PLAYLIST_ID_RE.match(playlist_id):
        return jsonify(error="invalid playlist id"), 400
    snapshot = _fast_playlist_snapshot(playlist_id)
    had_members = bool(snapshot and snapshot[1])
    members = _prepare_playlist_membership(playlist_id)
    if members is None:
        return jsonify(ok=False, error="could not refresh playlist metadata; YouTube returned no members and no local metadata was available"), 503
    return jsonify(ok=True, playlist_id=playlist_id, members=len(members),
                   repaired=bool(members and not had_members))


@app.post("/api/media/playlists/<string:playlist_id>/finish")
def api_media_playlist_finish(playlist_id: str):
    """Queue each incomplete member independently so one unavailable video cannot block the playlist."""
    if not PLAYLIST_ID_RE.match(playlist_id):
        return jsonify(error="invalid playlist id"), 400
    if not any(item["playlist_id"] == playlist_id for item in catalog.playlists(CATALOG_DB)):
        return jsonify(error="playlist not found"), 404
    members = catalog.playlist_video_ids(CATALOG_DB, playlist_id)
    try:
        cards, _total = catalog.library_cards(CATALOG_DB)
    except (sqlite3.Error, OSError, ValueError):
        cards = []
    playable = {
        card["video_id"] for card in cards
        if card.get("files", {}).get("video") and card.get("files", {}).get("audio")
    }
    queued = already = skipped = 0
    for member in members:
        video_id = member["video_id"]
        if video_id in playable:
            skipped += 1
            continue
        _item_id, was_already = _enqueue_target(video_id, "video", status="pending")
        queued += int(not was_already)
        already += int(was_already)
    if members:
        _queue_wakeup.set()
    return jsonify(ok=True, playlist_id=playlist_id, members=len(members),
                   queued=queued, already_queued=already, skipped_complete=skipped)


@app.get("/api/media/playlists/<string:playlist_id>.m3u")
def api_media_playlist_m3u(playlist_id: str):
    if not PLAYLIST_ID_RE.match(playlist_id):
        return jsonify(error="invalid playlist id"), 400
    mode = request.args.get("mode", "video").strip().lower()
    if mode not in {"video", "audio"}:
        return jsonify(error="mode must be video or audio"), 400
    members = catalog.playlist_video_ids(CATALOG_DB, playlist_id)
    videos_by_id = {item["video_id"]: item for item in _catalog_library_or_scan()}
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
def api_media_tags():
    cached = _read_derived_cache(_TAGS_CACHE)
    if isinstance(cached, dict) and "tags" in cached:
        return jsonify(cached)
    try:
        grouped = catalog.library_tags(CATALOG_DB)
        if grouped.get("tags"):
            _write_derived_cache(_TAGS_CACHE, grouped)
            return jsonify(grouped)
    except (sqlite3.Error, OSError, ValueError):
        pass
    grouped = _scan_tags()
    _write_derived_cache(_TAGS_CACHE, grouped)
    return jsonify(grouped)


@app.get("/api/media/resolve/<string:video_id>")
def api_media_resolve(video_id: str):
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(error="invalid video id"), 400

    video = _resolve_media_by_video_id(vid)
    if not video or not video.get("player_url"):
        return jsonify(result=False, video_id=vid, reason="metadata exists but no playable media file is present"), 404

    return jsonify(result=True, video=video)


@app.get("/api/downloads/status")
def api_downloads_status():
    return jsonify(_download_status_snapshot())


@app.get("/api/queue")
def api_queue_list():
    items = _repair_queue_kinds(catalog.queue_items(CATALOG_DB))
    with _lock:
        for item in items:
            item["pause_requested"] = int(item["id"]) in _pause_requests
            item["stopping"] = int(item["id"]) in _cancel_requests
    return jsonify(items=items,
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
        # YouTube watch URLs often contain both v= and list=. When a playlist
        # is present, queue the playlist and ignore the individual video.
        match = re.search(r"[?&]list=([A-Za-z0-9_-]{2,128})", raw)
        target = _normalize_playlist_id(match.group(1)) if match else None
        kind = "playlist"
        if not target:
            match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", raw)
            target = _normalize_id(match.group(1)) if match else None
            kind = "video"
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


@app.post("/api/queue/<int:item_id>/pause")
def api_queue_pause(item_id: int):
    for item in catalog.queue_items(CATALOG_DB, 2000):
        if int(item["id"]) == item_id and item["status"] == "running":
            with _lock:
                _pause_requests.add(item_id)
            return jsonify(ok=True, id=item_id, status="pausing")
    return jsonify(ok=False, error="running queue item not found"), 404


@app.post("/api/queue/<int:item_id>/cancel")
def api_queue_cancel(item_id: int):
    for item in catalog.queue_items(CATALOG_DB, 2000):
        if int(item["id"]) == item_id and item["status"] == "running":
            with _lock:
                _cancel_requests.add(item_id)
                _remove_requests.add(item_id)
            return jsonify(ok=True, id=item_id, status="stopping", removed=True)
        if int(item["id"]) == item_id and item["status"] == "pending":
            catalog.queue_set_status(CATALOG_DB, item_id, "cancelled", finished_at=time.time())
            return jsonify(ok=True, id=item_id)
        if int(item["id"]) == item_id and item["status"] == "paused":
            catalog.queue_set_status(CATALOG_DB, item_id, "cancelled", finished_at=time.time(), error="removed by user")
            return jsonify(ok=True, id=item_id)
    return jsonify(ok=False, error="pending queue item not found"), 404


@app.post("/api/queue/<int:item_id>/stop")
def api_queue_stop(item_id: int):
    for item in catalog.queue_items(CATALOG_DB, 2000):
        if int(item["id"]) == item_id and item["status"] == "running":
            with _lock:
                _cancel_requests.add(item_id)
                _remove_requests.discard(item_id)
            # Keep the worker active until yt-dlp reaches its next callback,
            # but persist paused immediately so the scheduler cannot treat the
            # requested stop as a runnable item or make it look restarted.
            catalog.queue_set_status(
                CATALOG_DB, item_id, "paused",
                error="stop requested; finishing current boundary",
                finished_at=None,
            )
            return jsonify(ok=True, id=item_id, status="stopping")
    return jsonify(ok=False, error="running queue item not found"), 404


@app.post("/api/queue/start-all")
def api_queue_start_all():
    changed = catalog.queue_start_all(CATALOG_DB)
    _queue_wakeup.set()
    return jsonify(ok=True, started=changed)


@app.post("/api/queue/clear")
def api_queue_clear():
    with _lock:
        protected = set(_cancel_requests)
    cleared = catalog.queue_clear(CATALOG_DB, protected)
    return jsonify(ok=True, cleared=cleared)


@app.post("/api/queue/cancel-all")
def api_queue_cancel_all():
    cancelled = 0
    for item in catalog.queue_items(CATALOG_DB, 2000):
        if item["status"] in {"pending", "paused"}:
            catalog.queue_set_status(CATALOG_DB, int(item["id"]), "cancelled", finished_at=time.time(), error="cancelled by user")
            cancelled += 1
    return jsonify(ok=True, cancelled=cancelled)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    try:
        catalog.seed_settings_from_file(CATALOG_DB, SETTINGS_FILE)
    except (OSError, sqlite3.Error, ValueError):
        pass
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("default_download_mode") or "").strip().lower()
        if mode not in {"immediate", "queue"}:
            return jsonify(ok=False, error="default_download_mode must be immediate or queue"), 400
        catalog.set_setting(CATALOG_DB, "default_download_mode", mode)
        for key, maximum in (("max_concurrent_downloads", 10), ("max_concurrent_playlists", 5)):
            if key in payload:
                try:
                    value = int(payload[key])
                except (TypeError, ValueError):
                    return jsonify(ok=False, error=f"{key} must be an integer"), 400
                if not 1 <= value <= maximum:
                    return jsonify(ok=False, error=f"{key} must be between 1 and {maximum}"), 400
                catalog.set_setting(CATALOG_DB, key, str(value))
        try:
            catalog.backup_settings_to_file(CATALOG_DB, SETTINGS_FILE)
        except (OSError, sqlite3.Error, ValueError):
            app.logger.exception("Settings backup failed")
    return jsonify(default_download_mode=catalog.setting(CATALOG_DB, "default_download_mode", "immediate"),
                   max_concurrent_downloads=_queue_limit("video"),
                   max_concurrent_playlists=_queue_limit("playlist"))


@app.get("/api/media/details/<string:channel_id>/<string:video_id>")
def api_media_details(channel_id: str, video_id: str):
    if not PLAYLIST_ID_RE.match(channel_id) or not YOUTUBE_ID_RE.match(video_id):
        return jsonify(error="invalid id"), 400

    video_dir = (MERGED_DIR / channel_id / video_id).resolve()
    media_root, media_prefix = MERGED_DIR, "/media"
    if not video_dir.is_dir():
        video_dir = (LEGACY_MERGED_DIR / channel_id / video_id).resolve()
        media_root, media_prefix = LEGACY_MERGED_DIR, "/media-legacy"
    if not video_dir.is_dir():
        video_dir = (FALLBACK_DIR / channel_id / video_id).resolve()
        media_root, media_prefix = FALLBACK_DIR, "/media-fallback"
    if not video_dir.is_relative_to(media_root):
        abort(403)
    if not video_dir.is_dir():
        abort(404)

    files = [
        _media_file_entry(path, channel_id, video_id, media_prefix)
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
      getvidyt.download_youtube("<id>", audio_meta=True)

    Does NOT modify archive.txt.
    """
    vid = _normalize_id(video_id)
    if not vid:
        return jsonify(ok=False, error="invalid video id"), 400
    permanent = _permanent_failure(vid)
    if permanent:
        return jsonify(ok=False, error=f"not retryable: {permanent[0]} — {permanent[1]}"), 409
    if _media_needs_archive_retry(vid):
        _remove_from_archive(vid)

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
        if _media_needs_archive_retry(vid):
            _remove_from_archive(vid)
        item_id, already_queued = _enqueue_target(vid, "video", cookies_browser=browser)
        return jsonify(ok=True, id=vid, queue_id=item_id, started=not already_queued, authenticated=True)
    return api_youtube_get(video_id)


@app.get("/api/media/failures")
def api_media_failures():
    try:
        with sqlite3.connect(CATALOG_DB, timeout=30) as db:
            db.executescript(catalog.SCHEMA)
            rows = db.execute("""SELECT latest.video_id,latest.status,latest.reason,latest.raw_error,latest.retryable,latest.started_at,latest.finished_at
              FROM (SELECT a.*, ROW_NUMBER() OVER (PARTITION BY a.video_id ORDER BY a.finished_at DESC) rn FROM download_attempts a)
              AS latest
              WHERE latest.rn=1 AND latest.status='failed' AND NOT EXISTS
              (SELECT 1 FROM archive_entries e WHERE e.video_id=latest.video_id AND e.status='complete')
              ORDER BY latest.finished_at DESC""").fetchall()
    except sqlite3.OperationalError:
        return jsonify(error="download history is temporarily busy; try again", failures=[]), 503
    return jsonify(failures=[{"video_id":r[0],"status":r[1],"reason":r[2],"error":r[3],"retryable":bool(r[4]),"started_at":r[5],"finished_at":r[6],"age_restricted":r[2]=="age_restricted","authenticated":str(r[3] or '').startswith('[cookies]')} for r in rows])


@app.get("/api/media/download-history")
def api_media_download_history():
    """Return persistent completed and failed download attempts."""
    try:
        rows = catalog.download_attempts(CATALOG_DB, request.args.get("limit", 500))
    except (TypeError, ValueError):
        return jsonify(error="limit must be a number"), 400
    except sqlite3.OperationalError:
        return jsonify(error="download history is temporarily busy; try again", attempts=[]), 503
    return jsonify(attempts=rows)


@app.get("/api/system/status")
def api_system_status():
    """Return safe operational diagnostics without exposing cookie contents."""
    global _system_status_cache
    now = time.time()
    with _lock:
        if _system_status_cache and now - _system_status_cache[0] < 15:
            return jsonify(_system_status_cache[1])
    usage = shutil.disk_usage(Path.cwd())
    # Cookies live at data/cookies.txt in both host and container layouts.
    # The old root ./cookies.txt is kept as a legacy fallback candidate.
    cookie_candidates = [Path("./data/cookies.txt"), Path("./cookies.txt")]
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
    result = {"disk": {"free_bytes": usage.free, "total_bytes": usage.total, "free_percent": round(usage.free * 100 / usage.total, 1)}, "directories": {str(path): directory_size(path) for path in (MERGED_DIR, LEGACY_MERGED_DIR, FALLBACK_DIR, DATA_ROOT / "audio")}, "cookies": {"present": cookie.is_file(), "netscape_format": False, "youtube_domains": [], "count": 0, "expired": 0}}
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
    with _lock:
        _system_status_cache = (time.time(), result)
    return jsonify(result)


def _cache_file_status(path: Path) -> tuple[str, float | None]:
    try:
        return ("available", path.stat().st_mtime) if path.is_file() else ("missing", None)
    except OSError:
        return "unavailable", None


@app.get("/api/system/cache-status")
def api_system_cache_status():
    readiness, readiness_at = _cache_file_status(_STARTUP_READINESS)
    playlist_cache, playlist_cache_at = _cache_file_status(_PLAYLIST_INDEX_CACHE)
    verify_cache, verify_cache_at = _cache_file_status(_VERIFY_CACHE)
    cleanup_state = "cached" if _cleanup_report_cache and time.time() - _cleanup_report_cache[0] < 15 else "live/on demand"
    with _lock:
        system_cache_at = _system_status_cache[0] if _system_status_cache else None
    tags_cache, tags_cache_at = _cache_file_status(_TAGS_CACHE)
    exports_cache, exports_cache_at = _cache_file_status(_EXPORTS_CACHE)
    with _lock:
        catalog_operation = dict(_catalog_refresh_state)
    return jsonify(items=[
        {"key": "catalog", "endpoint": "/api/media/library", "source": "SQLite catalog", "state": catalog_operation.get("phase") or ("ready" if _catalog_ready.is_set() else "building"), "cache": readiness, "last_updated": readiness_at, "refreshable": True, "note": "Validated media/archive/descriptor fingerprint", "operation": catalog_operation},
        {"key": "playlist-index", "endpoint": "/api/media/playlists", "source": "Local playlist descriptors + SQLite", "state": "cached" if playlist_cache == "available" else "on demand", "cache": playlist_cache, "last_updated": playlist_cache_at, "refreshable": True, "note": "Names, members, ordering, and local availability"},
        {"key": "tags", "endpoint": "/api/media/tags", "source": "SQLite catalog", "state": "cached" if tags_cache == "available" else "on demand", "cache": tags_cache, "last_updated": tags_cache_at, "refreshable": True, "note": "Persisted by catalog fingerprint"},
        {"key": "exports", "endpoint": "/api/media/library/files* and exports", "source": "SQLite catalog", "state": "cached" if exports_cache == "available" else "on demand", "cache": exports_cache, "last_updated": exports_cache_at, "refreshable": True, "note": "Files and YouTube/ID exports share one fingerprinted cache"},
        {"key": "video-details", "endpoint": "/api/media/details and /api/media/resolve", "source": "SQLite + live video directory", "state": "live query", "cache": "none", "last_updated": None, "refreshable": True, "note": "Per-video reads stay current"},
        {"key": "cleanup-report", "endpoint": "/api/media/cleanup-report", "source": "Filesystem + FFprobe", "state": cleanup_state, "cache": "15-second memory cache", "last_updated": _cleanup_report_cache[0] if _cleanup_report_cache else None, "refreshable": True, "note": "Potentially heavy filesystem analysis"},
        {"key": "verification", "endpoint": "/api/media/cleanup/verify", "source": "Filesystem + FFmpeg + MD5", "state": "cached per file" if verify_cache == "available" else "on demand", "cache": verify_cache, "last_updated": verify_cache_at, "refreshable": True, "note": "Full verification is intentionally explicit"},
        {"key": "system-status", "endpoint": "/api/system/status", "source": "Disk/cookie scan", "state": "cached" if system_cache_at else "on demand", "cache": "15-second memory cache", "last_updated": system_cache_at, "refreshable": True, "note": "Directory sizes are refreshed periodically"},
        {"key": "queue", "endpoint": "/api/queue and /api/downloads/status", "source": "SQLite + in-memory workers", "state": "live query", "cache": "none", "last_updated": None, "refreshable": True, "note": "Must remain current"},
    ])


@app.get("/api/system/startup-history")
def api_system_startup_history():
    try:
        history = json.loads(_STARTUP_HISTORY.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            history = []
    except (OSError, TypeError, ValueError):
        history = []
    return jsonify(runs=history[-30:])


@app.post("/api/system/cache-refresh/<string:key>")
def api_system_cache_refresh(key: str):
    global _playlist_index_cache_key, _playlist_index_cache_cards, _cleanup_report_cache, _system_status_cache
    valid = {"catalog", "playlist-index", "tags", "exports", "video-details", "cleanup-report", "verification", "system-status", "queue"}
    if key not in valid:
        return jsonify(ok=False, error="unknown cache or endpoint group"), 404
    if key == "catalog":
        _catalog_ready.clear()
        _playlist_index_cache_key = None
        _playlist_index_cache_cards = None
        for path in (_STARTUP_READINESS, _PLAYLIST_INDEX_CACHE):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        threading.Thread(target=_refresh_catalog, kwargs={"source": "manual refresh"}, name="manual-catalog-refresh", daemon=True).start()
        return jsonify(ok=True, status="building", message="Catalog refresh started"), 202
    if key == "playlist-index":
        _playlist_index_cache_key = None
        _playlist_index_cache_cards = None
        try:
            _PLAYLIST_INDEX_CACHE.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify(ok=True, status="invalidated", message="Playlist index will rebuild on its next request")
    if key == "tags":
        try:
            _TAGS_CACHE.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify(ok=True, status="invalidated", message="Tags cache cleared; it will rebuild on its next request")
    if key == "exports":
        try:
            _EXPORTS_CACHE.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify(ok=True, status="invalidated", message="Export cache cleared; it will rebuild on its next request")
    if key == "cleanup-report":
        _cleanup_report_cache = None
        return jsonify(ok=True, status="invalidated", message="Cleanup report cache cleared")
    if key == "verification":
        try:
            _VERIFY_CACHE.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify(ok=True, status="invalidated", message="Verification cache cleared; next verification will recheck files")
    if key == "system-status":
        with _lock:
            _system_status_cache = None
        return jsonify(ok=True, status="invalidated", message="System status cache cleared")
    return jsonify(ok=True, status="live", message="This endpoint is live and has no persistent cache to clear")


@app.get("/api/youtube/status/<string:video_id>")
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


@app.post("/api/youtube/playlist/prepare/<string:playlist_id>")
def api_youtube_playlist_prepare(playlist_id: str):
    """Create playlist metadata if missing without starting downloads."""
    pid = _normalize_playlist_id(playlist_id)
    if not pid:
        return jsonify(ok=False, error="invalid playlist id"), 400
    descriptor_path = PLAYLIST_METADATA_DIR / f"{pid}.info.json"
    if descriptor_path.is_file():
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            if isinstance(descriptor, dict) and descriptor.get("_type") == "playlist":
                members = descriptor.get("entries") or []
                if not members:
                    recovered = _recover_playlist_members_from_media(pid)
                    if recovered:
                        title, webpage_url, members = recovered
                        _save_playlist_metadata(pid, title, webpage_url, members)
                        _record_playlist_membership_best_effort(pid, title, webpage_url, members)
                        return jsonify(ok=True, id=pid, prepared=True, existing=True,
                                       repaired=True, members=len(members))
                return jsonify(ok=True, id=pid, prepared=True, existing=True, members=len(members))
        except (OSError, ValueError):
            pass
    members = _prepare_playlist_membership(pid)
    if members is None:
        return jsonify(ok=False, error="could not read playlist metadata"), 503
    return jsonify(ok=True, id=pid, prepared=True, existing=False, members=len(members))


@app.get("/api/youtube/playlist/status/<string:playlist_id>")
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
