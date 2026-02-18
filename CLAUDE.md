# CLAUDE.md

## Project Overview

**dihi** is a YouTube video archive management system. It provides:

- A Python/Flask REST API that tracks which YouTube videos have been downloaded and triggers new downloads
- A Chrome/Edge browser extension (Manifest V3) that shows archive status as a badge on YouTube pages
- Docker Compose deployment with Gunicorn

---

## Repository Structure

```
dihi/
├── src/dihi/
│   ├── app3.py          # Active Flask application (production)
│   ├── app.py           # Older version — not used in production
│   ├── app2.py          # Older version — not used in production
│   ├── getvidyt.py      # Core download module (yt-dlp wrapper + post-processors)
│   ├── getit.py         # Minimal one-shot download script
│   └── __init__.py      # Empty
├── extension/           # Chrome/Edge browser extension (Manifest V3)
│   ├── manifest.json
│   ├── service_worker.js
│   ├── popup.html / popup.js
│   ├── options.html / options.js
│   └── icons/
├── youtube-id-server-checker/  # Duplicate of extension/ (legacy copy)
├── data/
│   ├── archive.txt      # Download archive (tracked by git as placeholder)
│   └── .gitkeep
├── Dockerfile
├── docker-compose.yaml
├── Makefile
├── requirements.txt
└── plan.md / plan.playlist.md  # Feature planning documents
```

---

## Key Conventions

### Active Files

- **`src/dihi/app3.py`** is the canonical Flask server. `app.py` and `app2.py` are older iterations kept for reference — do not modify or run them.
- **`src/dihi/getvidyt.py`** is the core download module. All download logic lives here.
- The Docker container runs `app3:app` via Gunicorn.

### Archive Format

The archive file (`archive.txt` / `data/archive.txt`) uses yt-dlp's standard format:
```
youtube <video_id>
youtube <video_id>
```

The API parses this file and caches results by mtime to avoid repeated disk reads.

### YouTube Video ID Format

Video IDs are exactly 11 characters: `[A-Za-z0-9_-]{11}`. This regex is enforced at all API entry points before any processing.

### Output Directory Structure

Downloads go into `merged/` (bind-mounted to `data/merged/` in Docker):
```
merged/
└── <channel_id>/
    └── <video_id>/
        ├── CID_<channel_id>.<upload_date>.<title> [<id>].out.mkv   # video
        ├── CID_<channel_id>.<upload_date>.<title> [<id>].out.m4a   # clean audio
        ├── CID_<channel_id>.<upload_date>.<title> [<id>].out.info.json
        ├── CID_<channel_id>.<upload_date>.<title> [<id>].out.description
        ├── CID_<channel_id>.<upload_date>.<title> [<id>].out.png   # thumbnail
        └── CID_<channel_id>.<upload_date>.<title> [<id>].out.en.vtt  # subtitles
```

The `.out.` infix is part of `outtmpl` and ensures the format-ID stripping regex (`.f<id>`) works correctly.

---

## Backend: Flask API (`app3.py`)

### Endpoints

| Method | Path | Rate Limit | Description |
|--------|------|------------|-------------|
| `GET` | `/health` | 30/min | Health check; reports archive existence and active downloads |
| `GET` | `/api/youtube/<id>` | 60/min | Check if video is in archive → `{"result": true\|false}` |
| `POST` | `/api/youtube/get/<id>` | 10/min | Trigger download in background thread |
| `GET` | `/api/youtube/status/<id>` | 60/min | Poll download progress → `{"downloading": bool, "result": "completed"\|"failed"\|null, "in_archive": bool}` |

### Thread Safety

All shared state is protected by a single `threading.Lock()` (`_lock`):
- `_cached_mtime` / `_cached_ids` — archive file cache
- `_active_downloads` — set of video IDs currently downloading
- `_download_results` / `_result_timestamps` — completion status with 5-minute TTL

### Download Result Lifecycle

1. POST triggers background thread; video ID added to `_active_downloads`
2. GET `/status/<id>` returns `downloading: true` while thread is running
3. On completion, result is stored as `"completed"` or `"failed"` in `_download_results`
4. GET `/status/<id>` returns the result **once** (one-time consumption, then cleared)
5. Results expire after 300 seconds (5 minutes) regardless

### Limits

- Max 5 concurrent downloads (`MAX_CONCURRENT_DOWNLOADS`)
- Rate limits are per-IP via `flask-limiter` with in-memory storage
- CORS is fully open (`CORS(app)`)

---

## Download Module (`getvidyt.py`)

### Public API

```python
# Download a video or playlist (primary entrypoint)
download_youtube(target, *, merged_dir="merged", archive="archive.txt",
                 cookies_browser=None, no_js=False, extra_opts=None, quiet=False) -> int

# Build yt-dlp options dict without downloading
build_ydl_opts(merged_dir, archive, *, cookies_browser, no_js, extra_opts) -> dict

# Normalize video ID / URL / playlist ID to a full URL
to_youtube_url(user_input) -> str
```

### yt-dlp Format Selection

```
bestvideo[height<=1080][vcodec^=av01]+251 / bestvideo[height<=1080]+251 /
bestvideo[height<=1080]+bestaudio / best , 140/bestaudio
```

This prefers AV1 video merged with Opus audio (251) into MKV. If no video format is selected, it falls back to downloading a standalone m4a (format 140).

### Post-Processing Pipeline

Postprocessors run in this order:

1. **FFmpegThumbnailsConvertor** (`before_dl`) — converts thumbnails to PNG
2. **FFmpegEmbedSubtitle** — burns subtitle stream into the merged file
3. **EmbedThumbnail** — embeds cover art
4. **FFmpegMetadata** — embeds tags + chapters + info.json reference
5. **AudioMetadataPostProcessor** (`post_process`, custom) — creates clean audio copies

#### FFmpegMetadata m4a Patch

When the output is `.m4a`, yt-dlp's `FFmpegMetadataPP._options` does not specify a subtitle codec, causing ffmpeg to fail. `download_youtube()` patches this at runtime by injecting `-c:s copy` into the generated ffmpeg command. This patch is applied to each `FFmpegMetadataPP` instance found in `ydl._pps`.

### `AudioMetadataPostProcessor`

Creates a clean, metadata-enriched audio copy from the kept pre-merge audio stream (`.f<id>.<ext>` sidecar). The sidecar is preserved because `keepvideo: True` is set.

**Filename transformation**: `Title [id].out.f140.m4a` → `Title [id].out.m4a`

**Container-specific behavior**:

| Container | Cover Art | Lyrics |
|-----------|-----------|--------|
| `.m4a` / `.mp4` | Attached pic video stream (PNG/JPEG only) | `©lyr` via mutagen `MP4` |
| `.webm` / `.mkv` | ffmpeg `-attach` attachment | `LYRICS` ffmpeg metadata tag |
| `.opus` / `.ogg` | (none) | `LYRICS` Vorbis comment via mutagen |

**Subtitle extraction** (`_find_subtitle`):
1. Checks `info['requested_subtitles']` for `.vtt` or `.srt` files on disk
2. Falls back to globbing for `<stem>.*.vtt` then `<stem>.*.srt`

**Text parsing** (`_sub_to_text` / `_sub_to_text` module-level helper):
- Strips WEBVTT headers, timestamp lines, cue IDs, HTML tags
- Deduplicates consecutive identical lines (critical for auto-generated captions)
- Truncates to 64 KB at a line boundary with `\n[truncated]` appended

### JavaScript Challenge Solving

yt-dlp uses Deno via `yt-dlp-ejs` to solve YouTube's JS challenges:

```python
ydl_opts["js_runtimes"] = {"deno": {"path": deno_path}}
ydl_opts["remote_components"] = ["ejs:github", "ejs:npm"]
```

Deno is located by `_find_deno_path()`, which checks PATH, then `/root/.deno/bin/deno`, then `~/.deno/bin/deno`. Pass `no_js=True` to skip.

### Cookie Handling

- If `data/cookies.txt` exists (relative to the archive file), it is passed as `cookiefile`
- If `cookies_browser` is provided (e.g., `"firefox"`), `cookiesfrombrowser` is also set
- In Docker, `cookies.txt` is bind-mounted to `/app/cookies.txt`

---

## Browser Extension (`extension/`)

### Architecture

- **Manifest V3** service worker (no persistent background page)
- State is kept in-memory in the service worker (`tabState`, `downloadPollByTab` Maps)
- Configuration stored in `chrome.storage.sync`

### Badge States

| Badge | Color | Meaning |
|-------|-------|---------|
| `...` | Blue `#1a73e8` | Checking server |
| `OK` | Green `#00A000` | Video is archived |
| `NO` | Red `#D00000` | Video not in archive |
| `DL` | Yellow `#FFD000` | Download in progress |
| `ERR` | Red `#D00000` | API error |
| `—` | Gray `#808080` | YouTube page, no video ID |
| (empty) | Gray | Not a YouTube page |

### Message Protocol (popup ↔ service worker)

| `msg.type` | Direction | Description |
|------------|-----------|-------------|
| `GET_ACTIVE_STATUS` | popup → SW | Get current tab's archive/download state |
| `FORCE_RECHECK` | popup → SW | Force re-query the API, bypass debounce |
| `TRIGGER_DOWNLOAD` | popup → SW | POST to `/api/youtube/get/<id>` |

### Download Polling

When a download is triggered, the service worker creates a Chrome alarm (`poll_<tabId>`) that fires every 3 seconds (0.05 minutes). It polls `/api/youtube/status/<id>` and stops when `downloading` is `false`, then re-checks the archive status.

### Configurable Settings (`options.html`)

| Setting | Default | Description |
|---------|---------|-------------|
| `serverOrigin` | `https://dihi.i.apiskpis.com` | API base URL |
| `timeoutMs` | `6000` | Fetch timeout in milliseconds |
| `debounceMs` | `600` | Minimum time between checks for the same video |

---

## Development Workflow

### Local Development

```bash
# Create virtual environment and install dependencies
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install Deno (required for JS challenge solving)
curl -fsSL https://deno.land/install.sh | sh

# Run the server directly
python src/dihi/app3.py
# API available at http://localhost:5000
```

Note: The `Makefile` `run` target references `main.py` which does not exist. Use the command above instead.

### Docker Development

```bash
# Create required data files
mkdir -p data
touch data/archive.txt data/cookies.txt

# Build and start
docker-compose up -d --build

# View logs
docker-compose logs -f dihi

# Rebuild after code changes
docker-compose up -d --build
```

### Running the Downloader Directly

```bash
# From src/dihi/ with venv active:
python getvidyt.py dQw4w9WgXcQ
python getvidyt.py https://www.youtube.com/watch?v=dQw4w9WgXcQ
python getvidyt.py PLxxxxxxxx  # playlist

# With options:
python getvidyt.py dQw4w9WgXcQ --archive ./data/archive.txt --merged-dir ./data/merged
python getvidyt.py dQw4w9WgXcQ --no-js --quiet
```

### Installing the Browser Extension

1. Open Chrome/Edge → `chrome://extensions`
2. Enable "Developer mode"
3. Click "Load unpacked" → select `extension/`
4. Configure API URL via the extension options page if needed

---

## Dependencies

### Python (`requirements.txt`)

| Package | Purpose |
|---------|---------|
| `flask` | Web framework |
| `flask-cors` | CORS headers |
| `flask-limiter` | Rate limiting |
| `gunicorn` | Production WSGI server |
| `yt-dlp` | YouTube downloading |
| `yt-dlp-ejs` | JS challenge solver plugin for yt-dlp |
| `mutagen` | Audio metadata embedding (m4a, opus, ogg) |
| `curl_cffi` | HTTP client with browser fingerprinting |
| `certifi`, `requests`, `urllib3` | HTTP utilities |
| `websockets` | WebSocket support |
| `brotli` | Brotli decompression |

### System Dependencies (Docker)

- `ffmpeg` — media processing, metadata embedding, format conversion
- `Deno` — JavaScript runtime for YouTube challenge solving
- Python 3.12+

---

## Docker Configuration

### Volumes

| Host Path | Container Path | Description |
|-----------|---------------|-------------|
| `./data/archive.txt` | `/app/archive.txt` | Download archive |
| `./data/cookies.txt` | `/app/cookies.txt` | YouTube cookies (optional) |
| `./data/merged` | `/app/merged` | Downloaded files |

### Working Directory

The container's `WORKDIR` is `/app`. `app3.py` resolves `archive.txt` and `merged/` relative to the process working directory, so paths in Docker resolve to `/app/archive.txt` and `/app/merged/`.

### Gunicorn

```
gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 app3:app
```

2 workers × 4 threads = 8 concurrent requests. Each worker shares no memory, so `_active_downloads` and `_cached_ids` are per-worker. In practice this is fine since the archive file is the source of truth.

---

## Important Caveats for AI Assistants

1. **Do not modify `app.py` or `app2.py`** — these are older versions kept for reference. All changes to the API go in `app3.py`.

2. **The `.out.` infix is intentional** — it ensures the format-ID stripping regex (`\.f\d+(?=\.\w+$)`) only removes the last `.f<id>` segment and does not corrupt filenames with dots in channel names or titles.

3. **`keepvideo: True` is required** — `AudioMetadataPostProcessor` depends on yt-dlp keeping the pre-merge audio sidecars (`.f<id>.<ext>` files). Removing this option breaks audio copy creation.

4. **The m4a subtitle patch is a runtime monkey-patch** — it modifies a yt-dlp internal PP instance. If yt-dlp is updated and the method signature changes, this patch may need revisiting. The patch target is `FFmpegMetadataPP._options`.

5. **`_sub_to_text` exists at module level and in the plan** — the module-level `_sub_to_text()` function is the deduplication helper used by `AudioMetadataPostProcessor._embed()`. The `plan.md` describes more elaborate `_parse_subtitles` / `_to_lrc` methods that were planned but the simpler implementation was shipped instead.

6. **`youtube-id-server-checker/` is a duplicate** — it appears to be a legacy copy of `extension/`. The canonical extension is `extension/`.

7. **No test suite exists** — there are no unit or integration tests. Manual testing via `curl` and Docker is the current verification method.

8. **`data/cookies.txt` is gitignored** — never commit cookies. The file must be created manually on each deployment.
