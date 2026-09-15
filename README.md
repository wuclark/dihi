# dihi

A YouTube video archive management system. Downloads videos and playlists via yt-dlp, tracks them in a persistent archive, and exposes a REST API and browser extension for checking archive status.

## Features

- **CLI** — download videos and playlists, check archive status, embed audio metadata
- **REST API** — check archive status and trigger downloads over HTTP
- **Browser Extension** — Chrome/Edge badge overlay on YouTube pages
- **Docker** — production deployment via Docker Compose + Gunicorn

---

## CLI Quick Start

```bash
# One-time setup
make setup        # create venv and install dependencies
make dev-install  # install the `dihi` entry point

# Download a video or playlist
make dQw4w9WgXcQ                      # bare video ID
make PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm  # playlist ID
make "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Or use the dihi command directly
dihi download dQw4w9WgXcQ
dihi download PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm
dihi download dQw4w9WgXcQ --audio-meta   # also create clean .m4a with embedded metadata
dihi check dQw4w9WgXcQ                  # check local archive (no server needed)
dihi check dQw4w9WgXcQ --archive ./data/archive.txt
dihi audio-meta ./data/merged/           # post-process already-downloaded files
```

### CLI reference

```
dihi download <target> [options]
  target                YouTube video ID, playlist ID, or URL
  --archive PATH        yt-dlp archive file (default: archive.txt)
  --merged-dir PATH     output base directory (default: merged)
  --cookies-browser X   load cookies from browser profile, e.g. "firefox"
  --no-js               disable Deno/JS runtime
  --quiet               suppress yt-dlp output
  --audio-meta          create clean audio copies with embedded metadata

dihi check <target> [--archive PATH]
  exits 0 = found, 1 = not found, 2 = unrecognised ID/URL

dihi audio-meta <path> [--no-recursive]
  path can be a directory (scanned for .info.json files) or a single .info.json
```

---

## Docker Quick Start (REST API)

```bash
# 1. Create data directory
mkdir -p data
touch data/archive.txt data/cookies.txt   # cookies.txt optional

# 2. Start
docker-compose up -d --build

# 3. API available at http://localhost:5000
curl http://localhost:5000/health
```

---

## API Endpoints

| Method | Path | Rate Limit | Description |
|--------|------|------------|-------------|
| `GET` | `/health` | 30/min | Health check; reports archive existence and active downloads |
| `GET` | `/api/youtube/<id>` | 60/min | Check if a video is in the archive |
| `POST` | `/api/youtube/get/<id>` | 10/min | Trigger a video download in the background |
| `GET` | `/api/youtube/status/<id>` | 60/min | Poll video download progress |
| `POST` | `/api/youtube/playlist/get/<playlist_id>` | 5/min | Trigger a full playlist download |
| `GET` | `/api/youtube/playlist/status/<playlist_id>` | 60/min | Poll playlist download progress |
| `GET` | `/api/media/library` | 30/min | List archived media cards for the web UI |
| `GET` | `/api/media/resolve/<id>` | 60/min | Resolve one archived YouTube ID to its media record and preferred playback URL |
| `GET` | `/api/media/details/<channel_id>/<id>` | 60/min | Return files and metadata for one archived video |
| `GET` | `/api/media/tags` | 30/min | Return tag counts and tag-grouped videos |
| `GET` | `/api/downloads/status` | 60/min | Return active video/playlist downloads and recent completed/failed results |

Video IDs are exactly 11 characters (`[A-Za-z0-9_-]{11}`). Playlist IDs are 2–128 characters from the same alphabet.

### `/health`

```bash
curl http://localhost:5000/health
```
```json
{
  "ok": true,
  "archive_exists": true,
  "active_downloads": 0,
  "max_concurrent": 5,
  "active_playlist_downloads": 0,
  "max_concurrent_playlists": 2
}
```

### `GET /api/youtube/<id>` — check archive

```bash
curl http://localhost:5000/api/youtube/dQw4w9WgXcQ
```
```json
{"result": true}
```

### `POST /api/youtube/get/<id>` — trigger video download

```bash
curl -X POST http://localhost:5000/api/youtube/get/dQw4w9WgXcQ
```
```json
{"ok": true, "id": "dQw4w9WgXcQ", "started": true, "already_running": false}
```

Returns HTTP 429 when 5 concurrent downloads are already running.

### `GET /api/youtube/status/<id>` — poll video download progress

```bash
curl http://localhost:5000/api/youtube/status/dQw4w9WgXcQ
```

While downloading:
```json
{"downloading": true, "id": "dQw4w9WgXcQ", "result": null, "in_archive": false}
```

After completion:
```json
{"downloading": false, "id": "dQw4w9WgXcQ", "result": "completed", "in_archive": true}
```

`result` is `"completed"`, `"failed"`, or `null`. The result is consumed on first read and expires after 5 minutes.

### `POST /api/youtube/playlist/get/<playlist_id>` — trigger playlist download

```bash
curl -X POST http://localhost:5000/api/youtube/playlist/get/PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm
```
```json
{"ok": true, "id": "PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm", "started": true, "already_running": false}
```

Returns HTTP 429 when 2 concurrent playlist downloads are already running.

### `GET /api/youtube/playlist/status/<playlist_id>`

```bash
curl http://localhost:5000/api/youtube/playlist/status/PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm
```
```json
{"downloading": false, "id": "PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm", "result": "completed"}
```

### `GET /api/media/resolve/<id>` — resolve archived media

```bash
curl http://localhost:5000/api/media/resolve/dQw4w9WgXcQ
```
```json
{
  "result": true,
  "video": {
    "video_id": "dQw4w9WgXcQ",
    "channel_id": "UC...",
    "title": "Example",
    "files": {"video": "/media/UC.../dQw4w9WgXcQ/example.out.mkv"},
    "player_url": "/media/UC.../dQw4w9WgXcQ/example.out.mkv",
    "player_kind": "video"
  }
}
```

Returns HTTP 404 with `{"result": false, "video_id": "<id>"}` when the ID is not present in `merged/`.

### `GET /api/downloads/status` — download status

```bash
curl http://localhost:5000/api/downloads/status
```
```json
{
  "ok": true,
  "active": [
    {"id": "dQw4w9WgXcQ", "kind": "video", "status": "downloading", "active": true}
  ],
  "recent": [
    {"id": "PLexample", "kind": "playlist", "status": "completed", "active": false}
  ],
  "queue": {
    "empty": false,
    "message": "1 video(s) remaining in queue",
    "remaining_videos": 1,
    "remaining_playlists": 0,
    "remaining_total": 1
  },
  "counts": {"active": 1, "recent": 1}
}
```

The `/downloads` page polls this endpoint and shows active video/playlist downloads, recent completed or failed results, and a queue output. When there are no active downloads it shows `Queue Empty`. Queue state changes are also logged by the server.

---

## Browser Extension

Displays a badge on every YouTube video page showing its archive status.

| Badge | Color | Meaning |
|-------|-------|---------|
| `...` | Blue | Checking server |
| `OK` | Green | Video is archived |
| `NO` | Red | Video not in archive |
| `DL` | Yellow | Download in progress |
| `ERR` | Red | API error |
| `—` | Gray | YouTube page, no video ID |
| *(empty)* | Gray | Not a YouTube page |

### Installation

1. Open Chrome/Edge → `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select the `extension/` folder
4. Open the extension options page to set the API URL (default: `https://dihi.i.apiskpis.com`)

Options also control automatic behavior:

- Auto-download missing videos after a configurable per-video visit count.
- Open archived YouTube videos in the server UI instead of YouTube.

Visit counts are stored locally in the browser and reset when a video is found in the archive. When auto-download starts after a threshold match, the extension shows a notification. Before posting a download request, it resolves local media again and skips the request if the video is already downloaded. If the video is still missing on a later visit and its count is still at or above the threshold, the extension requests the download again.

The server UI accepts `/?play=<video_id>&autoplay=1` to open an archived video directly.

---

## Local Development

### Requirements

- Python 3.11+
- ffmpeg
- Deno (for YouTube JS challenge solving — `curl -fsSL https://deno.land/install.sh | sh`)

### Setup

```bash
make help         # list targets without installing anything
make setup        # create venv + install dependencies
make dev-install  # install the `dihi` CLI entry point (pip install -e .)
make test         # run the unit test suite

# Run the API server directly (not via Docker)
make run
```

Make uses the venv executables directly. To use `dihi` in your own shell, run `source venv/bin/activate` first or call `venv/bin/dihi`.

### Make targets

| Target | Description |
|--------|-------------|
| `make help` or `make` | List Make targets without installing dependencies |
| `make setup` | Create venv and install `requirements.txt` |
| `make dev-install` | Install `dihi` CLI entry point via `pip install -e .` |
| `make startup` | Show the command to activate the venv in your shell |
| `make run` | Start the Flask server from `src/dihi/app3.py` |
| `make test` | Run the unit test suite with coverage |
| `make clean` | Remove the venv |
| `make <id>` | Download a video or playlist (any unrecognised target) |

---

## Download Output

Videos are saved under `merged/` with a permanent two-level folder structure:

```
merged/
└── <channel_id>/                                                              # YouTube channel ID (never changes)
    ├── .channel_name                                                          # Channel display name history
    ├── .uploader_id                                                           # @handle history
    ├── .uploader_name                                                         # Uploader name history
    └── <video_id>/                                                            # YouTube video ID (never changes)
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.mkv       # Merged video (AV1 + Opus)
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.f140.m4a  # Pre-merge AAC audio sidecar
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.m4a       # Clean audio copy (--audio-meta)
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.info.json # Full yt-dlp metadata
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.formats.json # Available formats manifest
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.description
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.png       # Thumbnail (PNG)
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.en.vtt    # English subtitles
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.en-orig.vtt  # Non-English only
        ├── .title_name                                                        # Video title history
        └── .upload_date                                                       # Upload date history
```

### Why channel_id/video_id folders?

Channel names, @handles, and video titles all change over time. Using them as folder names causes fragmentation — new downloads land in a new path while old files stay in the old one. The `<channel_id>/<video_id>/` structure uses YouTube's own permanent identifiers so the archive never splits regardless of renames.

### Format selection and YouTube clients

The server intentionally requests separate video and audio streams so yt-dlp keeps raw sidecars and merges the final video to MKV:

```text
399+251/bestvideo[height<=1080][vcodec^=av01]+251/bestvideo[height<=1080]+251/bestvideo[height<=1080]+bestaudio,140/bestaudio
```

Do not add a `/best` progressive fallback if you need `.out.f<id>.*` sidecars and `.out.mkv`. A progressive fallback can select format `18`, which saves only `.out.mp4` and leaves no raw audio sidecar for `--audio-meta`.

If the strict format download fails, the downloader retries once into `data/bestfallback/` using a broader fallback chain:

```text
bestvideo+bestaudio/bv*+ba/best,140/bestaudio[ext=m4a]/bestaudio
```

The fallback retry is not capped at 1080p, uses best available audio instead of pinning Opus `251`, and forces yt-dlp's fallback sort toward highest resolution first (`res`, then `fps`, then bitrate). It uses its own archive file at `data/bestfallback/archive.txt`. That keeps the main archive clean: a fallback `.out.mp4` or other less-ideal result will not prevent a later strict-format download from succeeding into `merged/`.

Age-restricted videos require authenticated YouTube cookies. The downloader looks for cookies in this order:

1. `cookies.txt` next to the active archive file
2. `data/cookies.txt`
3. `./cookies.txt`

Use `make cookies` or `make cookies-browser` to refresh `data/cookies.txt`. If running through Docker, restart the service after refreshing cookies so the container sees the updated file.

The server also includes the `android_vr` YouTube client:

```python
{"youtube": {"player_client": ["android_vr", "web", "ios"]}}
```

That matters because some DASH formats, including `399` and `251` for `dQw4w9WgXcQ`, may be visible from the Android VR client while missing from the web/ios client set. TV clients are intentionally excluded because they trigger unsupported EJS challenge paths.

When cookies are active, yt-dlp skips `android_vr` and `ios` because those clients do not support cookies. In that case the server uses `["web", "web_safari"]`; `web_safari` exposes higher HLS formats for age-gated videos where the plain `web` client may only expose format `18` at 360p.

For a one-off CLI equivalent:

```bash
yt-dlp \
  -f "399+251/bestvideo[height<=1080][vcodec^=av01]+251/bestvideo[height<=1080]+251/bestvideo[height<=1080]+bestaudio,140/bestaudio" \
  --extractor-args "youtube:player_client=android_vr,web,ios" \
  --merge-output-format mkv \
  --keep-video \
  dQw4w9WgXcQ
```

When re-testing a video that already downloaded as `.out.mp4`, remove its `youtube <video_id>` line from `data/archive.txt` and delete the existing `data/merged/<channel_id>/<video_id>/` directory before downloading again. `download_archive` and `nooverwrites` are designed to preserve prior downloads.

Human-readable names are tracked in the dot-files alongside the content instead.

### Metadata sidecar files

Each dot-file is an append-only timestamped log. A new line is written only when the value changes:

```
2026-04-29T12:34:56Z Kurzgesagt – In a Nutshell
2026-06-01T09:15:00Z Kurzgesagt — In a Nutshell
```

This preserves the full history of observed values and when each change was first detected.

### Subtitle strategy

`en` requests English subtitles. yt-dlp automatically prefers manually uploaded captions over auto-generated ones when both exist. `en-orig` captures the native-language auto-generated transcript for non-English videos. At most 2 subtitle files are written per video.

To backfill subtitles for already-downloaded videos without re-downloading:

```bash
dihi download <id> --archive /dev/null --merged-dir /tmp/throwaway
# or via Python:
```
```python
from getvidyt import download_youtube
download_youtube(url, extra_opts={"skip_download": True, "download_archive": None,
                                  "subtitleslangs": ["en", "en-orig"]})
```

### Embedded metadata

The merged `.mkv` contains embedded subtitle streams, cover art, and full metadata tags. With `--audio-meta`, a clean `.out.m4a` copy is also produced with embedded cover art, chapter markers, and lyrics derived from the subtitle track.

---

## Docker Configuration

### Volumes

| Host Path | Container Path | Description |
|-----------|---------------|-------------|
| `./data/archive.txt` | `/app/archive.txt` | Download archive |
| `./data/cookies.txt` | `/app/cookies.txt` | YouTube cookies (optional) |
| `./data/merged` | `/app/merged` | Downloaded files |
| `./data/bestfallback` | `/app/data/bestfallback` | Fallback downloads and fallback archive |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | API server port |

---

## License

MIT
