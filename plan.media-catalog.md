# Media Catalog Plan

This documents the SQLite catalog. It is a rebuildable runtime index; the
filesystem and `.info.json` files remain authoritative.

## Purpose

Large archives need indexed searching, artist/channel and album pages, counts,
and server-side pagination instead of loading every card into one response.

## Metadata scan

The initial read-only scan walks `merged/<channel>/<video>/`, loads each
`.info.json`, and records stable IDs, metadata, local files, and tags. It does
not modify media or metadata. Display fallbacks are:

```text
artist = artist -> uploader -> channel -> channel_id
album  = album -> playlist_title -> "Uncategorized"
title  = track -> title -> video_id
```

The current archive has uploader/channel data but no explicit artist or album
fields.

## Storage responsibilities

| Data | SQLite catalog | Disk archive | Authority / purpose |
|---|---|---|---|
| Full yt-dlp metadata | `videos.metadata_json` copy | `<video>/*.info.json` | Disk is authoritative; SQL copy is queryable/cacheable |
| Title, uploader, channel, dates, duration | Indexed columns in `videos` | Also present in `.info.json` and filenames/history files | Disk metadata is authoritative; columns speed sorting/filtering |
| Artist and album fallbacks | `videos.artist`, `videos.album` | Usually derived from `.info.json` uploader/channel/playlist fields | Rebuilt from metadata; missing values are allowed |
| Tags | `video_tags` rows | `metadata_json.tags` in `.info.json` | SQL is for indexed tag queries; JSON preserves original list |
| Local file names, server URLs, sizes, mtimes, extensions, format IDs, and kinds | `videos.files_json` copy | Actual media, subtitle, thumbnail, and sidecar files | Disk is authoritative; SQL provides links and describes availability |
| Subtitle languages | Included in `files_json` and metadata copy | Actual `.vtt`/`.srt` files | A file on disk proves it was downloaded |
| Video/audio/subtitle bytes | Not stored in SQLite | `.mkv`, `.m4a`, `.webm`, `.vtt`, etc. | Disk only; never duplicate media in SQL |
| Archive membership | Not duplicated as media | `archive.txt` and downloaded directories | Existing archive/download logic remains authoritative |

Deleting `data/media-catalog.db` removes only the index. A later scan rebuilds
it from the disk files and `.info.json` metadata.

## Catalog design

The implementation scans both `merged/` and `data/bestfallback/` and stores the
source root with every file. Store the rebuildable database at
`data/media-catalog.db` (runtime data, never
Git or the image). Proposed tables are `videos`, `video_tags`, and
`media_files`, with indexes for artist, album, channel, dates, title, and ID.
Include a schema version and scan timestamp.

## Migration and rollback

1. Create the database beside the existing data; never move or rewrite media.
2. Scan existing directories and `.info.json` files read-only.
3. Compare catalog IDs and counts with the filesystem.
4. Enable catalog queries behind a configuration flag.
5. Keep the filesystem scanner available during the first release.

Download attempts are retained in `download_attempts` with normalized reason,
raw error, retryability, and timestamps. Partial files and subtitle/file
presence are represented in each video's `files_json`.

Rollback is disabling catalog reads or deleting `data/media-catalog.db`. The
`merged/` tree, archive files, `.info.json` metadata, and existing APIs remain
usable, and a later scan can rebuild the catalog.

## Planned pages and APIs

```text
GET /artists
GET /artists/<artist>
GET /albums
GET /albums/<album>
GET /api/media/artists
GET /api/media/artists/<artist>?page=1&per_page=50&sort=date-desc
GET /api/media/albums/<album>?page=1&per_page=50
```

The existing library endpoint can gain optional pagination and sort parameters
without changing its current default response.
