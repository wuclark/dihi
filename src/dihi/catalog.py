"""Rebuildable SQLite index for local media metadata.

The filesystem and yt-dlp info JSON remain authoritative. This database is a
runtime acceleration index and can be deleted and rebuilt at any time.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT NOT NULL, source_root TEXT NOT NULL, channel_id TEXT NOT NULL, title TEXT,
  artist TEXT, album TEXT, uploader TEXT, upload_date TEXT, duration REAL,
  metadata_json TEXT NOT NULL, files_json TEXT NOT NULL, scanned_at REAL NOT NULL,
  formats_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(video_id, source_root)
);
CREATE TABLE IF NOT EXISTS video_tags (video_id TEXT NOT NULL, tag TEXT NOT NULL,
  PRIMARY KEY(video_id, tag));
CREATE INDEX IF NOT EXISTS idx_videos_artist ON videos(artist);
CREATE INDEX IF NOT EXISTS idx_videos_album ON videos(album);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_date ON videos(upload_date);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON video_tags(tag);
CREATE TABLE IF NOT EXISTS download_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL,
  status TEXT NOT NULL, reason TEXT, raw_error TEXT, retryable INTEGER NOT NULL DEFAULT 1,
  started_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_attempts_video ON download_attempts(video_id, finished_at);
CREATE TABLE IF NOT EXISTS archive_entries (
  video_id TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at REAL NOT NULL
);
"""


def _info(path: Path) -> dict[str, Any]:
    for candidate in sorted(path.glob("*.info.json")):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8", errors="ignore"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def _formats(path: Path) -> list[dict[str, Any]]:
    for candidate in sorted(path.glob("*.formats.json")):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(value, list):
                return value
            return value.get("formats", []) if isinstance(value, dict) else []
        except (OSError, ValueError):
            return []
    return []


def _files(path: Path, channel_id: str, video_id: str, source_root: str) -> dict[str, dict[str, Any]]:
    result = {}
    for item in path.iterdir():
        if item.is_file():
            extension = item.suffix.lower()
            kind = (
                "video" if extension in {".mkv", ".mp4", ".webm"} else
                "audio" if extension in {".m4a", ".opus", ".mp3"} else
                "subtitle" if extension in {".vtt", ".srt"} else
                "thumbnail" if extension in {".png", ".jpg", ".jpeg", ".webp"} else
                "metadata" if extension == ".json" else
                "description" if extension == ".description" else "file"
            )
            match_format = re.search(r"\.out\.f(\d+)\.[^.]+$", item.name)
            media_prefix = (
                "/media-fallback" if source_root == "bestfallback" else
                "/media-legacy" if source_root == "legacy" else "/media"
            )
            entry: dict[str, Any] = {
                "url": (
                    f"{media_prefix}/{quote(channel_id, safe='')}/"
                    f"{quote(video_id, safe='')}/{quote(item.name, safe='')}"
                ),
                "source_root": source_root,
                "name": item.name,
                "size": item.stat().st_size,
                "mtime": item.stat().st_mtime,
                "extension": extension.lstrip("."),
                "format_id": match_format.group(1) if match_format else None,
                "kind": kind,
            }
            if extension in {".vtt", ".srt"}:
                match = re.search(r"\.out\.([^.]*)\.(?:vtt|srt)$", item.name)
                entry["subtitle_language"] = match.group(1) if match else None
            result[item.name] = entry
    return result


def refresh(merged_dir: Path | list[Path], database: Path, archive: Path | None = None) -> int:
    """Scan all local video directories and return the indexed video count."""
    roots = [Path(merged_dir)] if isinstance(merged_dir, (str, Path)) else [Path(p) for p in merged_dir]
    database.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        if "source_root" not in {row[1] for row in db.execute("PRAGMA table_info(videos)")}:  # rebuild old schema
            db.execute("DROP TABLE IF EXISTS videos")
            db.executescript(SCHEMA)
        if "formats_json" not in {row[1] for row in db.execute("PRAGMA table_info(videos)")}:
            db.execute("ALTER TABLE videos ADD COLUMN formats_json TEXT NOT NULL DEFAULT '[]'")
        for root in roots:
          # ``merged/`` and legacy ``data/merged/`` share a basename.
          source_root = "legacy" if root.as_posix().rstrip("/").endswith("data/merged") else root.name
          for channel_dir in sorted(root.iterdir()) if root.is_dir() else []:
            if not channel_dir.is_dir():
                continue
            for video_dir in sorted(channel_dir.iterdir()):
                if not video_dir.is_dir():
                    continue
                info = _info(video_dir)
                video_id = video_dir.name
                uploader = info.get("uploader") or info.get("channel") or channel_dir.name
                artist = info.get("artist") or uploader
                album = info.get("album") or info.get("playlist_title") or "Uncategorized"
                db.execute("""INSERT INTO videos
                  (video_id, source_root, channel_id, title, artist, album, uploader, upload_date,
                  duration, metadata_json, files_json, scanned_at, formats_json)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'), ?)
                  ON CONFLICT(video_id, source_root) DO UPDATE SET
                   channel_id=excluded.channel_id, title=excluded.title,
                   artist=excluded.artist, album=excluded.album, uploader=excluded.uploader,
                   upload_date=excluded.upload_date, duration=excluded.duration,
                   metadata_json=excluded.metadata_json, files_json=excluded.files_json,
                   scanned_at=excluded.scanned_at, formats_json=excluded.formats_json""", (
                    video_id, source_root, channel_dir.name, info.get("title") or video_id,
                    str(artist), str(album), str(uploader), info.get("upload_date"),
                    info.get("duration"), json.dumps(info, ensure_ascii=False, default=str),
                    json.dumps(_files(video_dir, channel_dir.name, video_id, source_root), ensure_ascii=False),
                    json.dumps(_formats(video_dir), ensure_ascii=False, default=str),
                ))
                db.execute("DELETE FROM video_tags WHERE video_id = ?", (video_id,))
                for tag in info.get("tags") or []:
                    db.execute("INSERT OR IGNORE INTO video_tags VALUES (?, ?)", (video_id, str(tag)))
                count += 1
        if archive and Path(archive).is_file():
            db.execute("DELETE FROM archive_entries")
            rows = db.execute("SELECT video_id, files_json FROM videos").fetchall()
            indexed = {}
            for video_id, files_json in rows:
                indexed.setdefault(video_id, []).append(json.loads(files_json))
            for line in Path(archive).read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split()
                if len(parts) < 2 or parts[0].lower() != "youtube":
                    continue
                video_id = parts[1]
                file_sets = indexed.get(video_id, [])
                files = {name: item for group in file_sets for name, item in group.items()}
                has_primary = any(item.get("kind") in {"video", "audio"} for item in files.values())
                has_partial = any(name.endswith((".part", ".ytdl")) for name in files)
                status = "complete" if has_primary else "interrupted" if has_partial else "missing"
                db.execute("INSERT OR REPLACE INTO archive_entries VALUES (?, ?, strftime('%s','now'))", (video_id, status))
        db.commit()
    return count


def record_attempt(database: Path, video_id: str, status: str, reason: str | None,
                   raw_error: str | None, retryable: bool, started_at: float, finished_at: float) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO download_attempts(video_id,status,reason,raw_error,retryable,started_at,finished_at) VALUES (?,?,?,?,?,?,?)",
                   (video_id, status, reason, raw_error, int(retryable), started_at, finished_at))
        db.commit()
