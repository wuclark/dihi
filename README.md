# dihi

A YouTube video archive management system. Downloads videos and playlists via yt-dlp, tracks them in a persistent archive, and exposes a REST API and browser extension for checking archive status.

## Features

- **CLI** — download videos and playlists, check archive status, embed audio metadata
- **PO Token provider** — automatically supplies per-video YouTube GVS tokens for reliable `mweb` downloads
- **REST API** — check archive status and trigger downloads over HTTP
- **Browser Extension** — Chrome/Edge badge overlay on YouTube pages
- **Docker** — production deployment via Docker Compose + Gunicorn

---

## CLI Quick Start

```bash
# One-time setup
make setup        # create venv and install dependencies
make dev-install  # install the `dihi` entry point

# Download a video or playlist (Make starts the PO Token service automatically)
make dQw4w9WgXcQ                      # bare video ID
make PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm  # playlist ID
make "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# For direct CLI downloads or local server-triggered downloads, start it first
make pot-provider
venv/bin/dihi download dQw4w9WgXcQ
venv/bin/dihi download PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm
venv/bin/dihi download dQw4w9WgXcQ --audio-meta  # tag copies of kept audio streams
venv/bin/dihi check dQw4w9WgXcQ                  # check local archive (no server needed)
venv/bin/dihi check dQw4w9WgXcQ --archive ./data/archive.txt
venv/bin/dihi audio-meta ./data/media-strict/  # post-process host CLI downloads
```

Docker must be running for the local PO Token service used by `make <video ID or URL>`. A direct CLI download needs `make pot-provider` first; the full Docker stack starts the service automatically.

### CLI reference

```
dihi download <target> [options]
  target                YouTube video ID, playlist ID, or URL
  --archive PATH        yt-dlp archive file (default: data/archive.txt)
  --merged-dir PATH     output base directory (default: data/media-strict)
  --cookies-browser X   load cookies from browser profile, e.g. "firefox"
  --no-js               disable Deno/JS runtime
  --quiet               suppress yt-dlp output
  --audio-meta          create tagged copies of kept audio streams when available

dihi check <target> [--archive PATH]
  exits 0 = found, 1 = not found, 2 = unrecognised ID/URL

dihi audio-meta <path> [--no-recursive]
  path can be a directory (scanned for .info.json files) or a single .info.json
```

---

## Docker Quick Start (REST API)

```bash
# 1. Create bind-mount data files
make data

# 2. Start
docker compose up -d --build

# 3. API available at http://localhost:5000
curl http://localhost:5000/health
```

Compose starts the PO Token provider with the app and publishes its service port only on host loopback.

---

## API Endpoints

| Method | Path | Rate Limit | Description |
|--------|------|------------|-------------|
| `GET` | `/health` | 30/min | Health check; reports archive existence and active downloads |
| `GET` | `/extension.zip` | — | Download the current browser extension bundle |
| `GET` | `/api/youtube/<id>` | 60/min | Check if a video is in the archive |
| `POST` | `/api/youtube/get/<id>` | 10/min | Trigger a video download in the background |
| `POST` | `/api/youtube/retry/<id>` | 10/min | Resume or retry a failed/partial video download |
| `GET` | `/api/youtube/status/<id>` | 60/min | Poll video download progress |
| `POST` | `/api/youtube/playlist/get/<playlist_id>` | 5/min | Trigger a full playlist download |
| `POST` | `/api/youtube/playlist/prepare/<playlist_id>` | 5/min | Save playlist name and members without downloading |
| `GET` | `/api/youtube/playlist/status/<playlist_id>` | 60/min | Poll playlist download progress |
| `GET` | `/api/media/library` | 30/min | List archived media cards from the catalog (`?page=&per_page=&sort=&q=`; default returns all cards with `total`) |
| `GET` | `/api/media/library/files` | — | List every catalog-indexed library file and link |
| `GET` | `/api/media/library/files.txt` | — | Export every catalog-indexed library file link as text |
| `GET` | `/api/media/library/youtube.txt` | — | Export all catalog video and playlist YouTube links |
| `GET` | `/api/media/library/ids.txt` | — | Export all catalog video and playlist IDs |
| `GET` | `/playlists` | — | Browse downloaded playlists, play all locally, or load a playlist in VLC |
| `GET` | `/library-export` | — | Browse and export links for all indexed library files |
| `GET` | `/api/media/resolve/<id>` | 60/min | Resolve one archived YouTube ID to its media record and preferred playback URL |
| `GET` | `/api/media/details/<channel_id>/<id>` | 60/min | Return files and metadata for one archived video |
| `GET` | `/api/media/tags` | 30/min | Return tag counts and tag-grouped videos |
| `GET` | `/api/media/catalog` | 60/min | Paginated text catalog of videos, dates, sources, files, formats, subtitles, and latest failure reasons |
| `POST` | `/api/media/catalog/refresh` | 6/min | Rebuild the catalog and remove stale playlist/video rows |
| `GET` | `/api/media/cleanup-report` | 12/min | List cleanup candidates, estimated savings, and FFmpeg remux recoverability without deleting files |
| `GET` | `/api/media/cleanup/tasks/<task_id>` | — | Poll cleanup move/delete task progress and final result |
| `POST` | `/api/media/cleanup/verify` | 3/min | Full-decode media verification, skipped when the file MD5 is unchanged |
| `POST` | `/api/media/cleanup/retry-missing` | 6/min | Queue incomplete media while skipping permanent failures and complete files |
| `POST` | `/api/media/cleanup/delete-legacy-matches` | 10/min | Delete only legacy conflict files proven identical to primary files |
| `POST` | `/api/media/cleanup/delete-legacy-different-all` | 3/min | Delete all differing legacy conflict files |
| `POST` | `/api/media/cleanup/delete-empty-legacy` and `/api/media/cleanup/delete-empty-legacy-all` | 10/3 min | Remove empty legacy video folders and empty channel parents |
| `GET` | `/wordcloud` / `/tagcloud` | — | Browse local description words and metadata tag frequencies |
| `GET` | `/api/downloads/status` | 60/min | Return active video/playlist downloads, recent results, and yt-dlp progress details |
| `GET` | `/queue` | — | Manage persistent pending, scheduled, and completed queue items |
| `GET` | `/api-docs` | — | Browse the available API endpoint reference |
| `GET` | `/sitemap` | — | Browse the site map |
| `GET` | `/cleanup` | — | Show a dry-run cleanup report and possible disk savings |
| `GET` | `/api/queue` | — | List persistent download queue items and default add behavior |
| `POST` | `/api/queue` | — | Add a video or playlist immediately, manually, or at a scheduled time |
| `POST` | `/api/queue/<item_id>/start` | — | Start a paused queue item immediately |
| `POST` | `/api/queue/<item_id>/cancel` | — | Cancel a pending queue item |
| `POST` | `/api/queue/start-all` | — | Start all paused queue items |
| `POST` | `/api/queue/cancel-all` | — | Cancel all pending and paused queue items |
| `GET/POST` | `/api/settings` | — | Read or set default add mode and concurrent video/playlist limits (backed up to `data/settings.json`) |
| `GET` | `/api/media/failures` | — | List failed download attempts with reasons and retryability |
| `GET` | `/api/media/download-history` | — | List persistent completed and failed download attempts |
| `GET` | `/api/media/playlists` | — | List downloaded playlists with total, locally available, and missing member counts |
| `GET` | `/api/media/playlists/<id>` | — | Return playlist metadata and ordered video members |
| `POST` | `/api/media/playlists/snapshot` | — | Save browser-captured autogenerated-playlist members as a timestamped local snapshot |
| `POST` | `/api/media/playlists/<id>/refresh` | 5/min | Refresh membership without deleting local media |
| `POST` | `/api/media/playlists/<id>/finish` | 10/min | Start downloading the playlist's incomplete members sequentially |
| `DELETE` | `/api/media/playlists/<id>` | — | Remove a local snapshot descriptor without deleting media |
| `GET` | `/api/media/playlists/<id>.m3u?mode=video\|audio` | — | Download a VLC-compatible video or audio-only playlist of local media URLs |

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

Returns HTTP 404 with `{"result": false, "video_id": "<id>"}` when the ID has no strict copy in `data/media-strict/`.

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

The `/downloads` page polls this endpoint and shows active video/playlist downloads, recent completed or failed results, persistent completed/failed attempt history, and a queue output. Playlist progress groups each item's video, audio, and metadata files beneath that item's title. The `/downloaded` page shows the persistent history in a scrollable panel. The `/playlists` page groups indexed videos by their saved YouTube playlist metadata, with ordered links, browser play-all, and separate video and audio-only M3U files for VLC. Each saved playlist shows locally available versus total members, such as `9/10 videos`, and incomplete playlists appear first. Playlist preflight records all members, including videos already in the archive. The `/video/<id>` page provides dedicated local playback, a collapsed full description, complete saved metadata and file inventory, and a direct VLC media URL. The `/status` page reports total disk capacity and usage for each media directory. The `/wordcloud` page generates on request and displays a spinner/progress state while descriptions are scanned and the cloud is arranged. A download is complete only when the archive entry and playable video/audio files are present; the catalog shows the missing-media reason and retry actions, including retry with browser cookies. Catalog rows with missing sidecars such as lyrics/subtitles, descriptions, thumbnails, or format manifests also provide a Retry missing files action. When there are no active downloads it shows `Queue Empty`. Queue state changes are also logged by the server.

The `/queue` page manages a persistent download queue. New downloads start immediately by default, but the setting can be changed to queue-only. Items can also be assigned a future start time; jobs interrupted by a server restart are returned to `paused` and do not resume until manually started. It also accepts one playlist URL, ID, or `playlist <ID>` line per entry for batch playlist JSON preparation or paused queue insertion, including CSV-style rows containing a watch URL; when both `v=` and `list=` are present, the playlist is loaded and the individual video is ignored. The **Preflight & save playlist JSON** button creates missing `data/playlists/<playlist_id>.info.json` files and skips valid existing descriptors, while the output panel reports each playlist's progress and result. The **Refresh playlist** action on `/playlists` explicitly fetches current membership. Adding a playlist first saves its name and ordered members without downloading; `/playlists` provides the explicit Download playlist action plus per-video Download buttons. Playlist jobs preflight their entries and download children sequentially, isolating fallback retries to failed videos instead of retrying successful playlist items.
Older queue rows that stored a playlist ID as a video job are automatically reclassified as playlist jobs when the queue is listed or scheduled.
Catalog responses also prefer the current live media directory when an older indexed row points at a stale legacy path, so thumbnails and media links remain usable while the catalog is repaired. Playlist preflight waits for the startup catalog scan to release its SQLite write lock before saving playlist membership.
The Tools page provides a “Rescan and repair links” button for refreshing catalog paths and metadata from the filesystem without deleting media.

The `/cleanup` Tools report is a dry run. It lists fallback duplicates and removable sidecars by YouTube ID, estimates total savings, checks raw sidecar recoverability by validating stream-copy remuxing from the final MKV with FFmpeg, checks final audio files—including WebM audio—with FFprobe, checks whether legacy folders can move to the default `data/media-strict/<channel>/<video_id>/` location without a target conflict, lists missing expected outputs, and links each reported file for inspection. Playlist descriptor directories (`_type: playlist`) are excluded from media inventory, missing-file checks, and legacy relocation checks. The report also provides confirmation-gated per-file delete, bulk delete, differing-legacy cleanup, empty-directory cleanup, and legacy-move actions for report-approved targets, with live task progress and completion notifications.

### Extension/API compatibility roadmap

The browser extension must mirror the active site flows and API endpoints. Changes to queue, playlist, playback, settings, or API response shapes should update the extension code, extension README, and extension version together, followed by endpoint-focused tests.

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
4. Open the extension options page to set the API URL (default: `https://dihi.i.apiskpis.com`; use `http://localhost:5000` for a local server)

Browsers do not install extensions directly from a localhost web page. Use
**Load unpacked** for local development; the installed extension can then call
the dihi server at `http://localhost:5000`.

When the local server is running, download the current extension bundle from
[`http://localhost:5000/extension.zip`](http://localhost:5000/extension.zip),
extract it, and select the extracted folder with **Load unpacked**.

Options also control automatic behavior:

- Auto-download missing videos after a configurable per-video visit count.
- Open archived YouTube videos in the server UI instead of YouTube.

Visit counts are stored locally in the browser and reset when a video is found in the archive. When auto-download starts after a threshold match, the extension shows a notification. Before posting a download request, it resolves local media again and skips the request if the video is already downloaded. If the video is still missing on a later visit and its count is still at or above the threshold, the extension requests the download again.

The server UI accepts `/video/<video_id>` to open an archived video directly; the library remains available at `/`. Library search and sort requests are handled by the catalog server-side, so the browser does not need to load the entire archive before filtering it. The Tools page links to `/library-tests`, a dedicated library search/sort/playlist test page with playlist selection by name, generated links, expected-result guidance, and live API JSON output.

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

To run the complete Docker stack (dihi plus the PO Token provider):

```bash
make docker-up
```

Stop it with `make docker-down`; follow logs with `make docker-logs`.

Run the Docker smoke checks with `make test-docker`. They verify health, the
library and catalog APIs, extension ZIP delivery, and serving a catalog media
file. The regular unit tests do not require Docker.

Docker dependency installation is cached separately from application source;
routine code and template edits therefore rebuild quickly. Changes to
`requirements.txt` or `pyproject.toml` invalidate that dependency layer; a
BuildKit pip cache avoids redownloading packages when that layer is retried.

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

Videos are saved under `data/media-strict/` with a permanent two-level folder structure:

```
data/media-strict/
└── <channel_id>/                                                              # YouTube channel ID (never changes)
    ├── .channel_name                                                          # Channel display name history
    ├── .uploader_id                                                           # @handle history
    ├── .uploader_name                                                         # Uploader name history
    └── <video_id>/                                                            # YouTube video ID (never changes)
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.mkv       # Merged video and audio
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.f399.mp4  # Kept raw video stream (format varies)
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.f251.webm # Kept raw audio stream (format varies)
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.m4a       # Separately downloaded AAC audio
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.webm      # Optional tagged audio copy (--audio-meta)
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.info.json # Full yt-dlp metadata
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.formats.json # Available formats manifest
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.description
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.webp      # Thumbnail (WebP or PNG)
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.en.vtt    # English subtitles
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.en-orig.vtt  # Non-English only
        ├── .title_name                                                        # Video title history
        └── .upload_date                                                       # Upload date history
```

The exact files vary by video and selected formats. `--audio-meta` creates tagged copies from kept raw audio streams when available; the default `.out.m4a` is the separately requested AAC download.

The planned SQLite catalog for large archives is documented in
[`plan.media-catalog.md`](plan.media-catalog.md). It will scan existing
`.info.json` files, provide indexed artist/channel and album pages, and remain
rebuildable. The filesystem and metadata files stay authoritative so deleting
or disabling the catalog is a safe rollback.

### Why channel_id/video_id folders?

Channel names, @handles, and video titles all change over time. Using them as folder names causes fragmentation — new downloads land in a new path while old files stay in the old one. The `<channel_id>/<video_id>/` structure uses YouTube's own permanent identifiers so the archive never splits regardless of renames.

### Format selection and YouTube clients

The server intentionally requests separate video and audio streams so yt-dlp keeps raw sidecars and merges the final video to MKV:

```text
399+251/bestvideo[height<=1080][vcodec^=av01]+251/bestvideo[height<=1080]+251/bestvideo[height<=1080]+bestaudio,140/bestaudio
```

Do not add a `/best` progressive fallback if you need `.out.f<id>.*` sidecars and `.out.mkv`. A progressive fallback can select format `18`, which saves only `.out.mp4` and leaves no raw audio sidecar for `--audio-meta`.

If the strict format download fails, the downloader retries once into `data/media-fallback/` using a broader fallback chain:

```text
bestvideo+bestaudio/bv*+ba/best,140/bestaudio[ext=m4a]/bestaudio
```

The fallback retry is not capped at 1080p, uses best available audio instead of pinning Opus `251`, and forces yt-dlp's fallback sort toward highest resolution first (`res`, then `fps`, then bitrate). It uses its own archive file at `data/media-fallback/archive.txt`. That keeps the main archive clean: a fallback `.out.mp4` or other less-ideal result will not prevent a later strict-format download from succeeding into `data/media-strict/`.

If both strict and fallback downloads return HTTP 403, refresh dependencies and retry. The Make download target starts the PO Token provider automatically:

```bash
make setup
make dev-install
make j5ky8YidivQ
```

Releases before 2026.08.19 have a known `android_vr` 403 issue. The `bgutil` provider supplies per-video GVS tokens to the `mweb` client, which yt-dlp recommends when direct streams are missing or forbidden. Run `make pot-provider` before calling `venv/bin/dihi download` or `make run` directly. Its HTTP port is published only on `127.0.0.1:4416`; Docker runs of `dihi` use the service address instead. To use another provider endpoint, set `DIHI_PO_TOKEN_PROVIDER_URL` to its URL; `make <video ID or URL>` then skips the local provider startup. A token can help with 403 errors but does not guarantee that YouTube will allow every download; see yt-dlp's [PO Token guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide).

Age-restricted videos require authenticated YouTube cookies. The downloader looks for cookies in this order:

1. `cookies.txt` next to the active archive file
2. `data/cookies.txt`
3. `./cookies.txt`

Use `make cookies` or `make cookies-browser` to refresh `data/cookies.txt`. If running through Docker, restart the service after refreshing cookies so the container sees the updated file.

The downloader tries `mweb` first with a GVS PO Token, then the existing clients:

```python
{"youtube": {"player_client": ["mweb", "android_vr", "web", "ios"]}}
```

That matters because some DASH formats, including `399` and `251` for `dQw4w9WgXcQ`, may be visible from the Android VR client while missing from the web/ios client set. TV clients are intentionally excluded because they trigger unsupported EJS challenge paths.

When cookies are active, yt-dlp skips `android_vr` and `ios` because those clients do not support cookies. In that case the server uses `["mweb", "web", "web_safari"]`; `web_safari` exposes higher HLS formats for age-gated videos where the plain `web` client may only expose format `18` at 360p.

For a one-off CLI equivalent:

```bash
venv/bin/yt-dlp \
  -f "399+251/bestvideo[height<=1080][vcodec^=av01]+251/bestvideo[height<=1080]+251/bestvideo[height<=1080]+bestaudio,140/bestaudio" \
  --extractor-args "youtube:player_client=mweb,android_vr,web,ios" \
  --merge-output-format mkv \
  --keep-video \
  dQw4w9WgXcQ
```

When re-testing a video that already downloaded as `.out.mp4`, remove its `youtube <video_id>` line from `data/archive.txt` and delete the existing `data/media-strict/<channel_id>/<video_id>/` directory before downloading again. `download_archive` and `nooverwrites` are designed to preserve prior downloads.

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

The merged `.mkv` contains embedded subtitle streams, cover art, and metadata tags. The separately downloaded `.out.m4a` receives title, artist, date, genre, description, source URL, and cover art; metadata is applied before artwork so the M4A `covr` atom survives yt-dlp's remux. The full YouTube metadata remains in `.info.json`. With `--audio-meta`, preserved raw audio sidecars can also produce clean tagged copies (for example, `.out.f251.webm` to `.out.webm`) with cover art, chapters, and subtitle-derived lyrics when available.

---

## Docker Configuration

### Volumes

| Host Path | Container Path | Description |
|-----------|---------------|-------------|
| `./data/archive.txt` | `/app/data/archive.txt` | Download archive |
| `./data/cookies.txt` | `/app/data/cookies.txt` | YouTube cookies (optional) |
| `./data/media-strict` | `/app/data/media-strict` | Downloaded files |
| `./data/media-fallback` | `/app/data/media-fallback` | Fallback downloads and fallback archive |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | API server port |

---

## License

MIT
