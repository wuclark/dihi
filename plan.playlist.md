# Plan: Add YouTube Playlist Download to API

## Overview

The download core (`getvidyt.py`) already fully supports playlists — `to_youtube_url()`
converts playlist IDs to URLs, `yes_playlist: True` is set, and `download_youtube()`
accepts any target. The work is entirely in `app3.py` (API layer) to expose playlist
downloads with proper validation, concurrency control, and progress tracking.

## Changes

### 1. Add playlist ID validation to `app3.py`

YouTube playlist IDs come in several prefixes (`PL`, `UU`, `LL`, `FL`, `OL`, `RD`,
`RDMM`, etc.) and are typically 13–34+ characters. Add a new regex and normalizer:

```python
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{13,}$")

def _normalize_playlist_id(raw: str) -> Optional[str]:
    vid = (raw or "").strip()
    if not vid or not PLAYLIST_ID_RE.match(vid):
        return None
    return vid
```

This deliberately excludes 11-char video IDs to avoid ambiguity.

### 2. Add playlist state tracking to `app3.py`

Add new in-memory data structures alongside the existing video ones:

```python
_active_playlist_downloads: dict[str, dict] = {}
# playlist_id -> {
#     "video_ids": [str, ...],        # full video list (from extract_info)
#     "total": int,                    # len(video_ids)
#     "completed": list[str],          # video IDs confirmed in archive
#     "failed": list[str],             # video IDs that failed
#     "status": "extracting" | "downloading" | "completed" | "failed",
# }

_playlist_results: dict[str, dict] = {}   # same structure, kept after completion
_playlist_result_timestamps: dict[str, float] = {}
```

### 3. Add playlist download worker to `app3.py`

Two-phase worker running in a daemon thread:

**Phase 1 — Extract playlist metadata (no download)**:
```python
with YoutubeDL({"extract_flat": "in_playlist", ...}) as ydl:
    info = ydl.extract_info(url, download=False)
    video_ids = [e["id"] for e in info["entries"] if e]
```

Store `video_ids` and `total` in state dict. Set status to `"extracting"` then `"downloading"`.

**Phase 2 — Download via existing `getvidyt.download_youtube()`**:
Call `getvidyt.download_youtube(playlist_id)` which handles the full download with
archive dedup, retries, etc.

After each archive.txt mtime change (polled periodically or via progress hooks),
refresh the cache and update `completed` count by intersecting `video_ids` with
`_cached_ids`.

After `download_youtube()` returns, do a final archive check and set status to
`"completed"` or `"failed"`.

### 4. Add new API endpoints to `app3.py`

#### `POST /api/youtube/playlist/<playlist_id>` — Start playlist download

- Validate playlist ID
- Check not already downloading
- Check concurrent download limit (playlist counts as 1)
- Spawn `_playlist_download_worker` in daemon thread
- Return `{"ok": true, "id": ..., "started": true/false}`
- Rate limit: 5/minute

#### `GET /api/youtube/playlist/status/<playlist_id>` — Check progress

- Return:
```json
{
  "id": "PLxxxxxxx",
  "downloading": true,
  "status": "extracting" | "downloading" | "completed" | "failed",
  "total": 42,
  "completed_count": 17,
  "failed_count": 2,
  "completed_video_ids": ["abc...", ...],
  "failed_video_ids": ["xyz...", ...]
}
```
- Rate limit: 60/minute

### 5. Update `/health` endpoint

Include playlist download count:
```python
"active_playlist_downloads": len(_active_playlist_downloads),
```

### 6. Concurrency considerations

- Playlist downloads count toward `MAX_CONCURRENT_DOWNLOADS` (each playlist = 1 slot).
- Since playlists are long-running, consider a separate limit: `MAX_CONCURRENT_PLAYLIST_DOWNLOADS = 2`.
- yt-dlp handles per-video sleep/throttle internally (`sleep_interval: 5`, `max_sleep_interval: 12`).

## Files Modified

| File | Change |
|------|--------|
| `src/dihi/app3.py` | All new endpoints, validation, state tracking, worker |

**No changes needed to `getvidyt.py`** — it already supports playlists.

## Testing approach

- Manual: `curl -X POST localhost:5000/api/youtube/playlist/<id>` then poll status
- Verify archive.txt gets populated with video IDs from the playlist
- Verify concurrent download limits are enforced
- Verify invalid playlist IDs return 400
