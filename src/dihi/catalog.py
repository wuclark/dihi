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

_SIDECAR_RE = re.compile(r"\.f\d+\.[^.]+$")

_LIBRARY_SORTS = {
    "date-asc": "upload_date ASC, video_id ASC",
    "date-desc": "upload_date DESC, video_id ASC",
    "title": "title COLLATE NOCASE ASC, video_id ASC",
    "artist": "artist COLLATE NOCASE ASC, title COLLATE NOCASE ASC, video_id ASC",
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


def _slim_files(files: dict[str, Any]) -> dict[str, str]:
    """Reduce per-file scan entries to slim card URLs.

    Mirrors the classification in ``app3._classify_file`` so catalog-backed
    cards keep the same ``files`` shape as filesystem scans. All inputs come
    from :func:`refresh`, which derives them from the on-disk directory
    listing; deleting the DB and re-running :func:`refresh` restores them.
    """
    out: dict[str, str] = {}
    for name in sorted(files):
        entry = files[name]
        if not isinstance(entry, dict):
            continue
        if _SIDECAR_RE.search(name):
            continue
        url = entry.get("url")
        if not url:
            continue
        ext = f".{str(entry.get('extension') or '').lower()}"
        if ext == ".mkv":
            out["video"] = url
        elif ext in (".mp4", ".webm") and "video" not in out:
            out["video"] = url
        elif ext == ".m4a" and "audio" not in out:
            out["audio"] = url
        elif ext == ".opus" and "audio" not in out:
            out["audio"] = url
        elif ext == ".png" and "thumbnail" not in out:
            out["thumbnail"] = url
        elif ext in (".jpg", ".jpeg", ".webp") and "thumbnail" not in out:
            out["thumbnail"] = url
        elif ext == ".vtt" and "subtitles" not in out:
            out["subtitles"] = url
        elif ext == ".srt" and "subtitles" not in out:
            out["subtitles"] = url
        elif ext == ".json":
            out["info_json"] = url
        elif name.endswith(".description"):
            out["description"] = url
    return out


def _deduped_rows(db: sqlite3.Connection, order: str, limit: int | None = None,
                  offset: int = 0) -> tuple[list[tuple], int]:
    """Return deduped video rows (preferring merged > legacy > bestfallback)."""
    total = db.execute("SELECT COUNT(DISTINCT video_id) FROM videos").fetchone()[0]
    query = """SELECT video_id, source_root, channel_id, title, upload_date, files_json, metadata_json
                 FROM (SELECT v.*, ROW_NUMBER() OVER (PARTITION BY video_id
                        ORDER BY CASE source_root WHEN 'merged' THEN 0 WHEN 'legacy' THEN 1 ELSE 2 END) AS rn
                       FROM videos v) WHERE rn = 1"""
    query += f" ORDER BY {order}"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        rows = db.execute(query, (limit, offset)).fetchall()
    else:
        rows = db.execute(query).fetchall()
    return rows, int(total or 0)


def library_cards(database: Path, page: int | None = None, per_page: int = 200,
                  sort: str = "date-desc") -> tuple[list[dict[str, Any]], int]:
    """Return slim library cards from the catalog without touching media files.

    Every field derives from :func:`refresh` inputs (directory names,
    ``*.info.json`` metadata, and the on-disk file listing), so the result is
    fully rebuildable by deleting the DB file and rescanning. ``scanned_at``
    timestamps are the only values that change across rebuilds.
    """
    order = _LIBRARY_SORTS.get(sort, _LIBRARY_SORTS["date-desc"])
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        if page is None:
            rows, total = _deduped_rows(db, order)
        else:
            page = max(1, int(page))
            per_page = max(1, min(int(per_page), 2000))
            rows, total = _deduped_rows(db, order, per_page, (page - 1) * per_page)
    items = []
    for video_id, source_root, channel_id, title, upload_date, files_json, _metadata_json in rows:
        try:
            files = json.loads(files_json or "{}")
        except ValueError:
            files = {}
        if not isinstance(files, dict):
            files = {}
        items.append({
            "video_id": video_id,
            "channel_id": channel_id,
            "source_root": source_root,
            "title": title or video_id,
            "date": upload_date,
            "files": _slim_files(files),
        })
    return items, total


def library_files(database: Path) -> list[dict[str, Any]]:
    """Return every indexed library file link from the catalog.

    Derived from the ``files_json`` snapshots written by :func:`refresh`,
    hence rebuildable from disk. Deduped to the preferred copy per video,
    matching the library card precedence.
    """
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        rows, _total = _deduped_rows(db, "video_id ASC")
    files: list[dict[str, Any]] = []
    for video_id, _source_root, _channel_id, title, _date, files_json, _metadata in rows:
        try:
            entries = json.loads(files_json or "{}")
        except ValueError:
            continue
        if not isinstance(entries, dict):
            continue
        for name in sorted(entries):
            entry = entries[name]
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            files.append({"video_id": video_id, "title": title or video_id, **entry})
    return files


def library_exports(database: Path) -> tuple[list[str], list[str]]:
    """Return YouTube links and ``video|playlist <id>`` lines from the catalog.

    Video URLs come from the indexed ``metadata_json`` copies (falling back to
    canonical watch URLs); playlist URLs come from both indexed video metadata
    and the ``playlists`` table, which itself is rebuilt from per-video
    ``playlist_*`` fields plus ``data/playlists/*.info.json`` descriptors.
    """
    links: list[str] = []
    ids: list[str] = []
    seen_videos: set[str] = set()
    seen_playlists: set[str] = set()
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        rows, _total = _deduped_rows(db, "video_id ASC")
        for video_id, _root, _channel, _title, _date, _files_json, metadata_json in rows:
            video_id = str(video_id or "").strip()
            if not video_id or video_id in seen_videos:
                continue
            seen_videos.add(video_id)
            try:
                info = json.loads(metadata_json or "{}")
            except ValueError:
                info = {}
            if not isinstance(info, dict):
                info = {}
            links.append(str(info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"))
            ids.append(f"video {video_id}")
            playlist_id = str(info.get("playlist_id") or "").strip()
            if playlist_id and playlist_id not in seen_playlists:
                seen_playlists.add(playlist_id)
                links.append(str(info.get("playlist_webpage_url")
                                 or f"https://www.youtube.com/playlist?list={playlist_id}"))
                ids.append(f"playlist {playlist_id}")
        try:
            for row in db.execute("SELECT playlist_id, webpage_url FROM playlists").fetchall():
                playlist_id = str(row[0] or "").strip()
                if not playlist_id or playlist_id in seen_playlists:
                    continue
                seen_playlists.add(playlist_id)
                links.append(str(row[1] or f"https://www.youtube.com/playlist?list={playlist_id}"))
                ids.append(f"playlist {playlist_id}")
        except sqlite3.Error:
            pass
    return links, ids


def library_tags(database: Path) -> dict[str, Any]:
    """Return tag counts and tag-grouped slim cards from the catalog.

    Tags come from the ``video_tags`` rows written by :func:`refresh` from
    each ``*.info.json`` ``tags`` list, so they rebuild from disk.
    """
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        video_rows, _total = _deduped_rows(db, "video_id ASC")
        cards = {}
        for video_id, source_root, channel_id, title, upload_date, files_json, _meta in video_rows:
            try:
                files = json.loads(files_json or "{}")
            except ValueError:
                files = {}
            cards[video_id] = {
                "video_id": video_id, "channel_id": channel_id, "source_root": source_root,
                "title": title or video_id, "date": upload_date,
                "files": _slim_files(files if isinstance(files, dict) else {}),
            }
        tag_rows = db.execute(
            """SELECT t.tag, t.video_id FROM video_tags t
               JOIN (SELECT video_id, MIN(CASE source_root WHEN 'merged' THEN 0
                        WHEN 'legacy' THEN 1 ELSE 2 END) AS rank
                     FROM videos GROUP BY video_id) best
                 ON best.video_id = t.video_id
               ORDER BY t.tag COLLATE NOCASE, t.video_id"""
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for tag, video_id in tag_rows:
        tag = str(tag or "").strip()
        card = cards.get(video_id)
        if not tag or card is None:
            continue
        grouped.setdefault(tag, []).append(card)
    return {
        "tags": [{"tag": tag, "count": len(items)}
                 for tag, items in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))],
        "videos_by_tag": grouped,
    }


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


# Single-object JSON backup for app_settings. JSON (not JSONL): settings are a
# small key/value map, not an event stream. The DB stays the read path; the
# file only seeds missing keys after a DB delete and receives a backup copy on
# every settings write.
_SETTINGS_BOUNDS = {
    "max_concurrent_downloads": 10,
    "max_concurrent_playlists": 5,
}


def _validated_setting(key: str, value: Any) -> str | None:
    text = str(value or "").strip()
    if key == "default_download_mode":
        return text if text in {"immediate", "queue"} else None
    if key in _SETTINGS_BOUNDS:
        try:
            number = int(text)
        except (TypeError, ValueError):
            return None
        if 1 <= number <= _SETTINGS_BOUNDS[key]:
            return str(number)
        return None
    return None


def read_settings_file(settings_file: Path) -> dict[str, str]:
    """Return validated settings from the JSON backup file ({} if absent)."""
    try:
        raw = json.loads(Path(settings_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        valid = _validated_setting(str(key), value)
        if valid is not None:
            cleaned[str(key)] = valid
    return cleaned


def write_settings_file(settings_file: Path, settings: dict[str, str]) -> None:
    """Atomically write validated settings to the JSON backup file."""
    cleaned = {}
    for key, value in settings.items():
        valid = _validated_setting(str(key), value)
        if valid is not None:
            cleaned[str(key)] = valid
    path = Path(settings_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def seed_settings_from_file(database: Path, settings_file: Path) -> int:
    """Fill missing DB settings from the JSON backup; never overwrite DB values.

    Returns the number of keys seeded. DB is authoritative: existing rows win
    so concurrent workers cannot clobber live settings with stale file data.
    """
    wanted = read_settings_file(settings_file)
    if not wanted:
        return 0
    seeded = 0
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        for key, value in wanted.items():
            row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            if row is None:
                db.execute("INSERT INTO app_settings(key,value) VALUES (?,?)", (key, value))
                seeded += 1
        db.commit()
    return seeded


def backup_settings_to_file(database: Path, settings_file: Path) -> None:
    """Copy all DB settings into the JSON backup file (best-effort)."""
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        rows = db.execute("SELECT key, value FROM app_settings").fetchall()
    write_settings_file(settings_file, {str(k): str(v) for k, v in rows})


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
