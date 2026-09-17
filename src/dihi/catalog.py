"""Rebuildable SQLite index for local media metadata.

The filesystem and yt-dlp info JSON remain authoritative. This database is a
runtime acceleration index and can be deleted and rebuilt at any time.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
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
CREATE TABLE IF NOT EXISTS playlists (
  playlist_id TEXT PRIMARY KEY, title TEXT NOT NULL, webpage_url TEXT,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_videos (
  playlist_id TEXT NOT NULL, video_id TEXT NOT NULL, playlist_index INTEGER,
  title TEXT, video_url TEXT, PRIMARY KEY (playlist_id, video_id),
  FOREIGN KEY (playlist_id) REFERENCES playlists(playlist_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_videos_video ON playlist_videos(video_id);
CREATE TABLE IF NOT EXISTS download_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
  scheduled_at REAL, started_at REAL, finished_at REAL, error TEXT,
  cookies_browser TEXT,
  UNIQUE(target, kind, status)
);
CREATE INDEX IF NOT EXISTS idx_download_queue_ready ON download_queue(status, scheduled_at, created_at);
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


# Directory names change over time (e.g. the data/ unification renamed
# ``merged/`` to ``media-strict/``); the API-facing source_root values stay
# stable so rows upsert instead of duplicating after a move.
_SOURCE_ROOT_ALIASES = {
    "media-strict": "merged",
    "media-legacy": "legacy",
    "media-fallback": "bestfallback",
}


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
                "/media-fallback" if source_root in ("bestfallback", "media-fallback") else
                "/media-legacy" if source_root in ("legacy", "media-legacy") else "/media"
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


def refresh(merged_dir: Path | list[Path], database: Path, archive: Path | None = None,
            playlist_metadata_dir: Path | None = None) -> int:
    """Scan all local video directories and return the indexed video count."""
    roots = [Path(merged_dir)] if isinstance(merged_dir, (str, Path)) else [Path(p) for p in merged_dir]
    database.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    known_roots: set[str] = set()
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        if "source_root" not in {row[1] for row in db.execute("PRAGMA table_info(videos)")}:  # rebuild old schema
            db.execute("DROP TABLE IF EXISTS videos")
            db.executescript(SCHEMA)
        if "formats_json" not in {row[1] for row in db.execute("PRAGMA table_info(videos)")}:
            db.execute("ALTER TABLE videos ADD COLUMN formats_json TEXT NOT NULL DEFAULT '[]'")
        for root in roots:
          # ``merged/`` and legacy ``data/merged/`` share a basename.
          source_root = "legacy" if root.as_posix().rstrip("/").endswith("data/merged") else _SOURCE_ROOT_ALIASES.get(root.name, root.name)
          known_roots.add(source_root)
          seen_video_ids: set[str] = set()
          for channel_dir in sorted(root.iterdir()) if root.is_dir() else []:
            if not channel_dir.is_dir():
                continue
            for video_dir in sorted(channel_dir.iterdir()):
                if not video_dir.is_dir():
                    continue
                info = _info(video_dir)
                if info.get("_type") == "playlist":
                    continue
                video_id = video_dir.name
                seen_video_ids.add(video_id)
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
                playlist_id = str(info.get("playlist_id") or "").strip()
                if playlist_id:
                    playlist_title = str(info.get("playlist_title") or playlist_id).strip()
                    playlist_url = str(
                        info.get("playlist_webpage_url")
                        or f"https://www.youtube.com/playlist?list={playlist_id}"
                    )
                    db.execute(
                        """INSERT INTO playlists(playlist_id,title,webpage_url,updated_at)
                           VALUES(?,?,?,strftime('%s','now'))
                           ON CONFLICT(playlist_id) DO UPDATE SET
                             title=excluded.title, webpage_url=excluded.webpage_url,
                             updated_at=excluded.updated_at""",
                        (playlist_id, playlist_title, playlist_url),
                    )
                    db.execute(
                        """INSERT INTO playlist_videos
                           (playlist_id,video_id,playlist_index,title,video_url)
                           VALUES(?,?,?,?,?)
                           ON CONFLICT(playlist_id,video_id) DO UPDATE SET
                             playlist_index=excluded.playlist_index,
                             title=excluded.title, video_url=excluded.video_url""",
                        (playlist_id, video_id, info.get("playlist_index"),
                         info.get("title") or video_id, info.get("webpage_url")),
                    )
                count += 1
          if seen_video_ids:
              placeholders = ",".join("?" for _ in seen_video_ids)
              db.execute(
                  f"DELETE FROM videos WHERE source_root = ? AND video_id NOT IN ({placeholders})",
                  (source_root, *sorted(seen_video_ids)),
              )
          else:
              db.execute("DELETE FROM videos WHERE source_root = ?", (source_root,))
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
        if playlist_metadata_dir and Path(playlist_metadata_dir).is_dir():
            for descriptor_path in sorted(Path(playlist_metadata_dir).glob("*.info.json")):
                try:
                    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(descriptor, dict) or descriptor.get("_type") != "playlist":
                    continue
                playlist_id = str(descriptor.get("id") or "").strip()
                if not playlist_id:
                    continue
                members = descriptor.get("entries") or []
                db.execute(
                    """INSERT INTO playlists(playlist_id,title,webpage_url,updated_at)
                       VALUES(?,?,?,?) ON CONFLICT(playlist_id) DO UPDATE SET
                       title=excluded.title, webpage_url=excluded.webpage_url,
                       updated_at=excluded.updated_at""",
                    (playlist_id, str(descriptor.get("title") or playlist_id),
                     descriptor.get("webpage_url"), descriptor.get("saved_at") or time.time()),
                )
                member_ids = [str(member.get("video_id") or member.get("id") or "").strip()
                              for member in members if isinstance(member, dict)]
                member_ids = [member_id for member_id in member_ids if member_id]
                if member_ids:
                    placeholders = ",".join("?" for _ in member_ids)
                    db.execute(
                        f"DELETE FROM playlist_videos WHERE playlist_id = ? AND video_id NOT IN ({placeholders})",
                        (playlist_id, *member_ids),
                    )
                else:
                    db.execute("DELETE FROM playlist_videos WHERE playlist_id = ?", (playlist_id,))
                for index, member in enumerate(members, 1):
                    if not isinstance(member, dict):
                        continue
                    video_id = str(member.get("video_id") or member.get("id") or "").strip()
                    if not video_id:
                        continue
                    db.execute(
                        """INSERT INTO playlist_videos
                           (playlist_id,video_id,playlist_index,title,video_url)
                           VALUES(?,?,?,?,?) ON CONFLICT(playlist_id,video_id) DO UPDATE SET
                           playlist_index=excluded.playlist_index, title=excluded.title,
                           video_url=excluded.video_url""",
                        (playlist_id, video_id, member.get("playlist_index") or index,
                         member.get("title") or video_id, member.get("video_url")),
                    )
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


def _ensure_queue_columns(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(download_queue)")}
    if "cookies_browser" not in columns:
        db.execute("ALTER TABLE download_queue ADD COLUMN cookies_browser TEXT")


def playlists(database: Path) -> list[dict[str, Any]]:
    """Return known playlists and their indexed video counts."""
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        _ensure_queue_columns(db)
        rows = db.execute(
            """SELECT p.playlist_id, p.title, p.webpage_url,
                      COUNT(pv.video_id), p.updated_at
                 FROM playlists p LEFT JOIN playlist_videos pv
                   ON pv.playlist_id = p.playlist_id
                GROUP BY p.playlist_id ORDER BY lower(p.title), p.playlist_id"""
        ).fetchall()
    return [
        {"playlist_id": r[0], "title": r[1], "webpage_url": r[2], "video_count": r[3], "updated_at": r[4]}
        for r in rows
    ]


def playlist_video_ids(database: Path, playlist_id: str) -> list[dict[str, Any]]:
    """Return playlist members in playlist order."""
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        _ensure_queue_columns(db)
        rows = db.execute(
            """SELECT video_id, playlist_index, title, video_url
                 FROM playlist_videos WHERE playlist_id = ?
                ORDER BY playlist_index IS NULL, playlist_index, video_id""",
            (playlist_id,),
        ).fetchall()
    return [{"video_id": r[0], "playlist_index": r[1], "title": r[2], "video_url": r[3]} for r in rows]


def record_playlist_membership(database: Path, playlist_id: str, title: str,
                               webpage_url: str | None, members: list[dict[str, Any]]) -> None:
    """Persist a playlist and its entries independently of video downloads."""
    # Startup catalog refreshes can hold SQLite's writer lock while scanning
    # a large library. Wait for that scan rather than reporting a misleading
    # playlist metadata failure after the network extraction succeeded.
    with sqlite3.connect(database, timeout=120) as db:
        db.executescript(SCHEMA)
        db.execute(
            """INSERT INTO playlists(playlist_id,title,webpage_url,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(playlist_id) DO UPDATE SET
               title=excluded.title, webpage_url=excluded.webpage_url,
               updated_at=excluded.updated_at""",
            (playlist_id, title or playlist_id, webpage_url, time.time()),
        )
        member_ids = [str(member.get("video_id") or "").strip() for member in members]
        if member_ids:
            placeholders = ",".join("?" for _ in member_ids)
            db.execute(f"DELETE FROM playlist_videos WHERE playlist_id = ? AND video_id NOT IN ({placeholders})",
                       (playlist_id, *member_ids))
        else:
            db.execute("DELETE FROM playlist_videos WHERE playlist_id = ?", (playlist_id,))
        for member in members:
            video_id = str(member.get("video_id") or "").strip()
            if not video_id:
                continue
            db.execute(
                """INSERT INTO playlist_videos(playlist_id,video_id,playlist_index,title,video_url)
                   VALUES(?,?,?,?,?) ON CONFLICT(playlist_id,video_id) DO UPDATE SET
                   playlist_index=excluded.playlist_index, title=excluded.title,
                   video_url=excluded.video_url""",
                (playlist_id, video_id, member.get("playlist_index"),
                 member.get("title") or video_id, member.get("video_url")),
            )
        db.commit()


def queue_add(database: Path, target: str, kind: str, scheduled_at: float | None = None,
              cookies_browser: str | None = None, status: str = "pending") -> int:
    """Persist a pending queue item and return its id."""
    now = time.time()
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        _ensure_queue_columns(db)
        db.execute(
            """INSERT INTO download_queue(target,kind,status,created_at,scheduled_at,cookies_browser)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (target, kind, status, now, scheduled_at, cookies_browser),
        )
        item_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()
    return int(item_id)


def queue_items(database: Path, limit: int = 500) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        _ensure_queue_columns(db)
        rows = db.execute(
            """SELECT id,target,kind,status,created_at,scheduled_at,started_at,finished_at,error,cookies_browser
                 FROM download_queue ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                 COALESCE(scheduled_at, created_at), id LIMIT ?""",
            (max(1, min(int(limit), 2000)),),
        ).fetchall()
    keys = ("id", "target", "kind", "status", "created_at", "scheduled_at", "started_at", "finished_at", "error", "cookies_browser")
    return [dict(zip(keys, row)) for row in rows]


def queue_set_status(database: Path, item_id: int, status: str, **fields: Any) -> None:
    allowed = {"scheduled_at", "started_at", "finished_at", "error"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    updates["status"] = status
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        _ensure_queue_columns(db)
        db.execute(f"UPDATE download_queue SET {assignments} WHERE id = ?", (*updates.values(), item_id))
        db.commit()


def queue_set_kind(database: Path, item_id: int, kind: str) -> None:
    """Repair a queued target whose kind was inferred incorrectly."""
    if kind not in {"video", "playlist"}:
        raise ValueError("invalid queue kind")
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        db.execute("UPDATE download_queue SET kind = ? WHERE id = ?", (kind, item_id))
        db.commit()


def setting(database: Path, key: str, default: str) -> str:
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(database: Path, key: str, value: str) -> None:
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES (?,?)", (key, value))
        db.commit()


def download_attempts(database: Path, limit: int = 500) -> list[dict[str, Any]]:
    """Return the durable download attempt history, newest first."""
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        rows = db.execute(
            """SELECT a.id, a.video_id, a.status, a.reason, a.raw_error,
                      a.retryable, a.started_at, a.finished_at,
                      COALESCE(v.title, a.video_id)
                 FROM download_attempts AS a
                 LEFT JOIN (
                   SELECT video_id, MAX(title) AS title
                     FROM videos GROUP BY video_id
                 ) AS v ON v.video_id = a.video_id
                ORDER BY COALESCE(a.finished_at, a.started_at) DESC, a.id DESC
                LIMIT ?""",
            (max(1, min(int(limit), 2000)),),
        ).fetchall()
        archived = db.execute(
            """SELECT v.video_id, COALESCE(MAX(v.title), v.video_id), MAX(v.scanned_at)
                 FROM videos AS v
                GROUP BY v.video_id"""
        ).fetchall()
    attempts = [
        {
            "attempt_id": row[0],
            "video_id": row[1],
            "status": row[2],
            "reason": row[3],
            "error": row[4],
            "retryable": bool(row[5]),
            "started_at": row[6],
            "finished_at": row[7],
            "title": row[8],
        }
        for row in rows
    ]
    known = {item["video_id"] for item in attempts}
    attempts.extend(
        {
            "attempt_id": f"archive:{video_id}",
            "video_id": video_id,
            "status": "completed",
            "reason": "archived media",
            "error": "",
            "retryable": False,
            "started_at": scanned_at,
            "finished_at": scanned_at,
            "title": title,
        }
        for video_id, title, scanned_at in archived
        if video_id not in known
    )
    attempts.sort(key=lambda item: (item["finished_at"] or item["started_at"], str(item["attempt_id"])), reverse=True)
    return attempts[: max(1, min(int(limit), 2000))]
