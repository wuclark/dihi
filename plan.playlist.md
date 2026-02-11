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

---

## Extension: Add "Download Playlist" Button

### Current extension behavior

- `extractYouTubeId()` only extracts the `v=` param (video ID) from YouTube URLs
- Popup shows: Video ID, status pill, Recheck / Download buttons
- Service worker tracks per-tab video state and polls `/api/youtube/status/<id>`
- Playlist pages (`youtube.com/playlist?list=PLxxx`) show badge "—" (no video ID found)

### 7. Add playlist ID extraction to `service_worker.js`

New function alongside `extractYouTubeId()`:

```javascript
function extractPlaylistId(urlString) {
  try {
    const url = new URL(urlString);
    const host = url.hostname.replace(/^www\./, "");
    if (host === "youtube.com" || host === "m.youtube.com") {
      return url.searchParams.get("list") || null;
    }
  } catch {}
  return null;
}
```

This captures the `list=` param from:
- `/playlist?list=PLxxxxxx` (dedicated playlist page)
- `/watch?v=abc&list=PLxxxxxx` (video playing within a playlist)

### 8. Extend tab state to track playlists in `service_worker.js`

Update `tabState` entries to include playlist info:

```javascript
// tabId -> { videoId, playlistId, isTrue, isPlaylistDownloading, lastCheckedAt }
```

Add a parallel `playlistPollByTab` map:
```javascript
const playlistPollByTab = new Map(); // tabId -> { playlistId, serverOrigin }
```

### 9. Add `startPlaylistDownloadFlow()` to `service_worker.js`

New function mirroring `startDownloadFlow()`:

- POST to `/api/youtube/playlist/<playlist_id>`
- On success, set badge to `"PL"` (yellow) and start polling alarm
- Poll `/api/youtube/playlist/status/<playlist_id>` every 3 seconds
- During polling, optionally update badge text with progress (e.g., `"3/42"`)
- On completion, show notification "Playlist download finished (42 videos)"

### 10. Add playlist-aware polling alarm handler

Extend the `chrome.alarms.onAlarm` listener to handle `plpoll_<tabId>` alarms:

```javascript
if (alarm.name.startsWith("plpoll_")) {
  // Poll /api/youtube/playlist/status/<id>
  // Update badge with progress: "5/20"
  // On status "completed" or "failed": clear alarm, notify, recheck tab
}
```

### 11. Update popup UI (`popup.html` + `popup.js`)

**HTML changes:**
- Add a new row below the existing Video ID row:
  ```html
  <div class="row" id="playlistRow" style="display:none">
    <div class="label">Playlist</div>
    <div class="value" id="playlistId">—</div>
  </div>
  ```
- Add a "Download Playlist" button next to existing buttons:
  ```html
  <div class="row btns">
    <button id="recheckBtn">Recheck</button>
    <button id="downloadBtn">Download</button>
    <button id="downloadPlaylistBtn" style="display:none">Download Playlist</button>
  </div>
  ```
- Add a playlist progress row (hidden by default):
  ```html
  <div class="row" id="playlistProgress" style="display:none">
    <div class="label">Playlist Progress</div>
    <div class="value"><span id="playlistProgressText">—</span></div>
  </div>
  ```

**JS changes (`popup.js`):**
- Update `GET_ACTIVE_STATUS` message to also return `playlistId` and `isPlaylistDownloading`
- Update `render()` to show/hide the playlist row and button:
  - Show "Download Playlist" button when `playlistId` is present and playlist is not already downloading
  - Show progress text when playlist is downloading
- Add click handler for `downloadPlaylistBtn` that sends `TRIGGER_PLAYLIST_DOWNLOAD` message
- Add a `TRIGGER_PLAYLIST_DOWNLOAD` message type in service worker's `onMessage` handler

### 12. Update service worker message handling

Add new message types to `chrome.runtime.onMessage`:

```javascript
if (msg?.type === "GET_ACTIVE_STATUS") {
  // ... existing code ...
  // ADD: playlistId, isPlaylistDownloading to response
}

if (msg?.type === "TRIGGER_PLAYLIST_DOWNLOAD") {
  // Start playlist download flow for active tab
}

if (msg?.type === "GET_PLAYLIST_STATUS") {
  // Fetch /api/youtube/playlist/status/<id> and return progress
}
```

### 13. Badge behavior on playlist pages

| Page type | Badge | Behavior |
|-----------|-------|----------|
| `/playlist?list=PLxxx` (no video) | `"PL"` gray | Click opens popup with Download Playlist |
| `/watch?v=abc&list=PLxxx` | `"OK"`/`"NO"` (video status) | Popup shows both video + playlist options |
| Playlist downloading | `"PL"` yellow | Popup shows progress |
| Playlist done | `"PL"` green | Notification sent |

---

## Files Modified

| File | Change |
|------|--------|
| `src/dihi/app3.py` | Playlist endpoints, validation, state tracking, worker |
| `extension/service_worker.js` | Playlist ID extraction, download flow, polling, messages |
| `extension/popup.html` | Playlist ID row, Download Playlist button, progress row |
| `extension/popup.js` | Playlist-aware rendering, new button handler, new messages |

**No changes needed to `getvidyt.py`** — it already supports playlists.

## Testing approach

- **API**: `curl -X POST localhost:5000/api/youtube/playlist/<id>` then poll status
- **API**: Verify archive.txt gets populated with video IDs from the playlist
- **API**: Verify concurrent download limits are enforced
- **API**: Verify invalid playlist IDs return 400
- **Extension**: Load unpacked in Edge, navigate to a YouTube playlist page, verify "Download Playlist" button appears
- **Extension**: Click Download Playlist, verify badge shows progress, notification on completion
- **Extension**: Navigate to a video within a playlist (`?v=...&list=...`), verify both buttons appear
