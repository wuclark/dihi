# AGENTS.md

Canonical instructions for coding agents working in this repository.

## Project Shape

`dihi` is a YouTube archive manager with three main surfaces:

- CLI/download code in `src/dihi/getvidyt.py` and `src/dihi/cli.py`
- Flask server in `src/dihi/app3.py`
- Browser extension in `extension/`

`src/dihi/app.py` and `src/dihi/app2.py` are older reference versions. Do not modify or run them unless explicitly asked.

## Keep This File Current

When a code change alters behavior, commands, endpoints, file layout, Docker/runtime config, test workflow, dependency requirements, or important caveats, update this file in the same change. If the change is user-facing, also update `README.md`. If a detail is only historical or exploratory, keep it out of this file.

Before finishing a substantial change, quickly check whether these sections still match the code:

- active entrypoints
- run/test commands
- API and UI routes
- download format logic
- output file layout
- Docker/Gunicorn config
- known caveats

## Active Entrypoints

- Server: `src/dihi/app3.py`
- Web UI templates: `src/dihi/templates/index.html`, `src/dihi/templates/tags.html`
- Downloader: `src/dihi/getvidyt.py`
- CLI: `src/dihi/cli.py`
- Browser extension: `extension/`
- Legacy duplicate extension: `youtube-id-server-checker/`

Docker runs `app3:app` with Gunicorn:

```bash
gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 8 app3:app
```

The Docker image includes `extension/` so `/extension.zip` works from the
container as well as from a source checkout.

The Dockerfile installs third-party requirements before copying application
source, so edits to Python, templates, or the extension reuse the dependency
layer. The pip wheel cache is persisted through BuildKit as well, so a failed
dependency build does not need to download every package again. Change
`requirements.txt` or `pyproject.toml` to intentionally rebuild that slower
layer.

## Common Commands

```bash
make help
make setup
make dev-install
make pot-provider
make docker-up
make docker-down
make docker-logs
make test-docker
make run
venv/bin/pytest
python src/dihi/app3.py
docker compose up -d --build
make git-add
make git-commit-push MSG="Describe the change"
```

`make help` works before setup and is the default Make target. `make setup` installs the pinned yt-dlp and PO Token plugin in the venv and writes `venv/.setup-complete` only after all installs succeed; a failed pip run remains retryable. `make dev-install` installs the CLI entry point. Both use venv executables directly. To activate the venv in your shell, run `source venv/bin/activate` yourself (or use `make startup` to display that command). `make <video ID or URL>` starts the Docker PO Token service before downloading unless `DIHI_PO_TOKEN_PROVIDER_URL` points to another provider. `make pot-provider` starts it explicitly for direct CLI calls or server-triggered downloads. `make run` starts the active server in `src/dihi/app3.py`.

`make docker-up` initializes bind-mount data files, then builds and starts the dihi server and PO Token provider. `make docker-down` stops the Compose services; `make docker-logs` follows their logs. `make git-add` intentionally excludes local runtime data paths: `data/**`, root `archive.txt`, root `cookies.txt`, and `audio/**`.

`make test-docker` starts the Compose stack and runs opt-in smoke tests against
the live HTTP service. The normal `venv/bin/pytest` suite remains Docker-free.

## Server Routes

Core UI/media routes:

- `GET /` renders the media library UI
- `GET /extension.zip` downloads the browser extension bundle for local installation
- `GET /tags` renders the tag browser UI
- `GET /downloads` renders the active/recent download status UI
- `GET /queue` renders the persistent manual/scheduled download queue, including one-per-line playlist batch input. Batch rows may be bare playlist IDs, `playlist <ID>` lines, or CSV-style YouTube watch URLs; `list=` takes precedence over `v=`.
- `GET /downloaded` renders completed and failed download history
- `GET /catalog` renders the paginated metadata/file catalog table
- `GET /library-export` renders links for all indexed library files and a text export
- `GET /wordcloud` renders a local description/lyrics word cloud
- `GET /tagcloud` renders a tag frequency cloud
- `GET /playlists` renders downloaded playlists with local play-all and VLC playlist actions
- `GET /video/<video_id>` renders a dedicated local video/audio detail page
- `GET /tools` renders the analysis/tools hub
- `GET /api-docs` renders the available API endpoint reference
- `GET /sitemap` renders the site map
- `GET /cleanup` renders the dry-run cleanup report
- `GET /status` renders disk and cookie diagnostics
- `GET /api/media/library` lists library cards
- `GET /api/media/library/files` lists every indexed library file with a link
- `GET /api/media/library/files.txt` exports every indexed library file link as text
- `GET /api/media/library/youtube.txt` exports all video and playlist YouTube links
- `GET /api/media/library/ids.txt` exports all video and playlist IDs
- `GET /api/media/details/<channel_id>/<video_id>` returns per-video files and metadata
- `GET /api/media/resolve/<video_id>` returns the archived media record and preferred playable URL for one YouTube ID
- `GET /api/media/tags` returns tag counts and tag-grouped videos
- `GET /api/media/playlists` lists downloaded playlists and member counts
- `GET /api/media/playlists/<playlist_id>` returns a named playlist and its ordered local video members
- `POST /api/media/playlists/<playlist_id>/refresh` refreshes membership without deleting local media
- `POST /api/media/playlists/<playlist_id>/finish` starts the playlist worker for incomplete members
- `GET /api/media/playlists/<playlist_id>.m3u?mode=video|audio` downloads a VLC-compatible local video or audio-only playlist file
- `GET /api/media/wordcloud/videos?word=<word>` returns archived video IDs containing a word in saved descriptions
- `GET /api/media/catalog` returns paginated catalog rows with metadata, local file/format details, and the latest failure reason for incomplete downloads
- `POST /api/media/catalog/refresh` rebuilds the filesystem-backed catalog and removes stale rows
- `GET /api/media/cleanup-report` lists cleanup candidates, estimated disk savings, FFmpeg remux recoverability checks, final-audio integrity checks, dry-run legacy relocation checks, complete folder inventories, and missing expected outputs without deleting files
- `POST /api/media/cleanup/delete`, `/api/media/cleanup/delete-all`, and `/api/media/cleanup/move-legacy` perform confirmation-gated cleanup actions only for report-approved targets
- `POST /api/media/cleanup/delete-legacy-matches` deletes only legacy conflict files whose MD5 matches the primary copy
- `POST /api/media/cleanup/delete-legacy-different-all` deletes all differing legacy conflict files
- `POST /api/media/cleanup/delete-empty-legacy` and `/api/media/cleanup/delete-empty-legacy-all` remove empty legacy video folders and empty channel parents
- Playlist descriptor directories (`.info.json` with `_type: playlist`) are excluded from cleanup media inventory, expected-file checks, and legacy relocation checks.
- Catalog responses re-read the live media directory when an indexed row points to a stale source, keeping thumbnail and media URLs usable before the persistent catalog is rebuilt.
- `GET /api/media/cleanup/tasks/<task_id>` reports cleanup action phase, progress, completion, or error
- Cleanup UI actions show live progress and a completion notification; bulk move/delete operations remain confirmation-gated.
- The Tools page includes a confirmation-free filesystem rescan button that refreshes catalog paths and links without deleting media.
- `POST /api/media/cleanup/verify` starts a full media verification pass cached by file MD5
- `POST /api/media/cleanup/retry-missing` queues incomplete media while skipping permanent failures and complete files
- Catalog rows with missing metadata, formats, thumbnails, descriptions, or subtitles/lyrics expose a “Retry missing files” action; sidecar retries bypass the download archive when needed.
- `GET /api/media/wordcloud` returns description word frequencies, filterable by tag or playlist
- `GET /api/media/failures` lists recorded failed download attempts and reasons
- `GET /api/media/download-history` lists persistent completed and failed download attempts
- `GET /api/system/status` reports disk capacity and safe cookie-file diagnostics without cookie values
- `GET /api/downloads/status` returns active video/playlist downloads, recent completed/failed results, queue summary fields including `Queue Empty` when idle, and yt-dlp progress details (`phase`, `percent`, `filename`, and recent `logs`)
- `GET /api/queue` lists persistent pending, running, completed, failed, and cancelled queue items
- `POST /api/queue` adds a video or playlist to the queue; the default is immediate start, configurable through `/api/settings`
- `POST /api/queue/<item_id>/start` starts a paused/scheduled queue item immediately
- `POST /api/queue/<item_id>/cancel` cancels a pending queue item
- Queue listing and scheduling repair old rows that stored playlist IDs as video jobs.
- `POST /api/queue/start-all` starts all paused queue items
- `POST /api/queue/cancel-all` cancels all pending and paused queue items
- `GET/POST /api/settings` reads or updates the default add behavior (`immediate` or `queue`) and concurrent video/playlist limits
- `GET /media/<path>` serves downloaded files with conditional/range-capable responses
- `GET /media-legacy/<path>` serves files from the legacy `data/merged` tree
- `GET /media-fallback/<path>` serves fallback-library files with conditional responses

The rebuildable SQLite catalog now scans both `merged/` and
`data/bestfallback/`, retaining source-root and per-file format/subtitle
details. It also stores playlist names and video memberships from each `.info.json`; a video can belong to multiple playlists. Download failures are classified and persisted as retry information. The catalog endpoint initializes its schema before reading so the UI can continue serving while the background filesystem scan is running. Playlist preflight waits for that startup scan's SQLite write lock when persisting playlist membership.
The planned indexed artist/album pages and safe rollback are documented in
[`plan.media-catalog.md`](plan.media-catalog.md). It is not required by the
current filesystem-backed library; it remains a rebuildable runtime index while
media and `.info.json` files stay authoritative.

Archive/download API routes:

- `GET /health`
- `GET /api/youtube/<video_id>`
- `POST /api/youtube/get/<video_id>`
- `POST /api/youtube/retry/<video_id>` resumes or retries a failed/partial download; add `?authenticated=1&browser=chrome` for browser-cookie authentication or omit `browser` to use `data/cookies.txt`
- `GET /api/youtube/status/<video_id>`
- `POST /api/youtube/playlist/get/<playlist_id>`
- `POST /api/youtube/playlist/prepare/<playlist_id>` reads and stores playlist membership without starting downloads
- `GET /api/youtube/playlist/status/<playlist_id>`

The extension depends on the `/api/youtube/*` endpoints and `/api/media/resolve/<video_id>`. Keep their response shapes stable unless updating the extension in the same change.

## Data And Output Layout

Archive files use yt-dlp format:

```text
youtube <video_id>
```

Docker bind mounts:

- `./data/archive.txt` -> `/app/archive.txt`
- `./data/cookies.txt` -> `/app/cookies.txt`
- `./merged` -> `/app/merged`
- `./data/merged` -> `/app/data/merged` (legacy media compatibility)
- `./data/bestfallback` -> `/app/data/bestfallback`

Downloads are stored under:

```text
merged/
└── <channel_id>/
    └── <video_id>/
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.mkv
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.f399.mp4
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.f251.webm
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.m4a
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.info.json
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.formats.json
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.description
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.webp (or .out.png)
        └── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.en.vtt
```

The exact set varies by source video and available formats. The `<channel_id>/<video_id>/` directory structure is intentional because those IDs are stable across title/channel renames. The catalog also stores playlist membership independently, so preflighted playlist entries link existing media without downloading it again. Preflight-only playlists are saved as rebuildable `data/playlists/<playlist_id>.info.json` descriptors and imported when the SQLite catalog is rebuilt.

The default `.out.m4a` is the separately requested AAC audio download. `--audio-meta` can make additional clean, tagged copies from kept raw audio sidecars, such as `.out.f251.webm` to `.out.webm`.

`merged/` is the strict output tree and uses root `archive.txt`. Failed strict downloads retry into `data/bestfallback/` with its own `archive.txt`; the fallback archive does not mark a strict download complete. `keepvideo: True` preserves raw `.f<id>.<ext>` streams alongside the merged MKV, so disk use includes duplicate media data.

## Download Logic

Current yt-dlp format string:

```text
399+251/bestvideo[height<=1080][vcodec^=av01]+251/bestvideo[height<=1080]+251/bestvideo[height<=1080]+bestaudio,140/bestaudio
```

Meaning:

- Prefer exact video format `399` plus audio format `251`
- Fallback to best AV1 video up to 1080p plus `251`
- Fallback to best video up to 1080p plus `251`
- Fallback to best video up to 1080p plus best audio
- Also download `140` audio, falling back to best audio

`merge_output_format` is `mkv`, and `keepvideo: True` is required because audio metadata creation depends on preserved `.f<id>.<ext>` sidecars.

## Postprocessing Notes

- Thumbnails are converted to PNG for embedding when needed; the original WebP sidecar can remain on disk.
- `FFmpegMetadata` runs before `EmbedThumbnail`; reversing them drops the M4A `covr` artwork atom during metadata remuxing.
- Metadata, chapters, thumbnails, subtitles, descriptions, info JSON, and format manifests are written when available.
- `AudioMetadataPostProcessor` can create clean audio copies with embedded metadata.
- A video is marked complete only when its archive entry and both playable video and audio files exist. Incomplete attempts are persisted as failures with a reason and remain retryable from the catalog, including browser-cookie authentication.
- The cleanup report uses `ffprobe` and FFmpeg stream-copy remux checks; the Docker image installs both as part of the `ffmpeg` package. Results are cached per source folder/container during each report.
- The word cloud keeps lyric vocabulary but filters common URL terms and description credit/promotion boilerplate (such as video, lyrics, director, producer, and subscribe). `/wordcloud` generates only when its Generate button is pressed and shows a spinner/progress state while descriptions are scanned and the layout is arranged.
- Server-triggered video and playlist downloads call `download_youtube(..., audio_meta=True)`. Playlist downloads preflight entries and invoke the downloader per child video, so fallback is isolated to failed children and successful strict children are not re-downloaded into `data/bestfallback/`.
- Deno/`yt-dlp-ejs` is used for YouTube JS challenge solving unless `no_js=True`.
- yt-dlp must be at least 2026.08.19; older releases can return HTTP 403 for `android_vr` video streams. `make setup` refreshes the pinned version after `requirements.txt` changes.
- The `bgutil-ytdlp-pot-provider` plugin supplies per-video GVS PO Tokens for the `mweb` client. `make pot-provider` starts its Docker service, and `make <video ID or URL>` starts it automatically. Compose publishes its port only on `127.0.0.1:4416`; the app container uses `DIHI_PO_TOKEN_PROVIDER_URL=http://bgutil-provider:4416`. The host default is `http://127.0.0.1:4416`.
- Host workflow: `make setup`, `make dev-install`, then `make <video ID or URL>`. `make setup` installs the Python plugin but does not start its Docker service. Direct `venv/bin/dihi download` and `make run` need `make pot-provider` first if they will download. `docker compose up -d --build` starts the provider with the app through `depends_on`; run `make data` first for bind-mount files.

## UI Notes

The web UI is part of `app3.py`, not a separate server yet. It lists the archive, batches thumbnail rendering, supports video/audio playback, exposes VLC URLs, loads per-video details lazily, and includes `/queue` for persistent manual/scheduled downloads. The default add behavior is immediate; changing the queue setting holds new items until manually started. Jobs interrupted by a server restart are recovered as paused queue items and do not auto-resume. The queue page's playlist batch tool accepts bare IDs, `playlist <ID>` lines, URLs, and CSV-style watch rows; its preparation button creates missing playlist descriptor JSON files, skips valid existing descriptors, and reports each playlist's progress in the output box. `/playlists` provides named playlist membership, ordered video links, per-video download buttons, browser play-all, and separate VLC video/audio-only M3U files; its Refresh playlist action explicitly re-fetches membership. `/downloads` shows active/recent download status, persistent attempt history, failed-download retry actions, and a remaining queue output. Playlist progress groups each item's video, audio, and metadata files beneath that item's title. `/downloaded` shows the same persistent completed/failed attempt history in a scrollable panel. `/video/<video_id>` shows the saved description collapsed by default, full metadata, every indexed file with size/type, playable video/audio links, and the VLC media URL. The main library restores active/recent queue entries from the server after refresh. Playlist rows expose child video links and `playlist_index/total` progress as yt-dlp encounters each entry. Queue state changes are also written to the server log, including `Queue Empty` when all active downloads finish.
All non-library pages use the shared `templates/_header.html` navigation. The library header keeps primary library/download links; `/tools` is the hub for the catalog, tags, word cloud, tag cloud, Edge extension, status, and downloaded-history tools.

The browser extension tracks per-video YouTube visit counts in `chrome.storage.local`. When enabled, it auto-downloads missing videos after the configured visit threshold and notifies when the automatic request starts. It calls `/api/media/resolve/<video_id>` before posting a download request and skips the request if local media already exists. Counts reset only once the video is found in the archive, so still-missing videos at or above the threshold are requested again on later visits. Archived playback can use a preflight redirect, an in-page local player replacement, or an ask prompt. In-page replacement keeps YouTube around the player but still loads YouTube page resources; local autoplay starts muted because audible autoplay requires browser permission or a user gesture. The dihi downloads UI exposes source YouTube URLs as copy-only controls instead of direct links.

Browser playback of `.mkv` is inconsistent across browsers and codecs. Keep the VLC URL path available when changing playback behavior.

## Tests

Run:

```bash
venv/bin/pytest
```

The current suite is pure unit tests: no network, no real yt-dlp download, no ffmpeg integration, and no browser automation. `make test-docker` additionally runs opt-in smoke tests against the live Compose HTTP service; its library-endpoint budget is generous because `/api/media/library` is an unpaginated full-filesystem scan (see Roadmap).

## Roadmap / TODO

- Keep the browser extension mirrored with the active site flows and API endpoints. When queue, playlist, playback, settings, or response-shape behavior changes, update the extension code, extension README, and extension version together, then test the extension against the documented endpoints.
- Serve `/api/media/library` (and its `/files`, `*.txt`, playlist-detail, and resolve callers) from the SQLite catalog, paginate it, or otherwise stop the per-request full `merged/` rescan that inlines descriptions and `info.json` per video. Check the web UI and extension against any response-shape change in the same update.

## Coding Caveats

- Keep `app3.py` as the active server unless explicitly asked to split the UI/API.
- Do not commit cookies or downloaded media.
- Do not stage or commit local archive/runtime data such as `data/archive.txt`, root `archive.txt`, or `audio/**`.
- Preserve `/api/youtube/*` compatibility for the extension.
- Be careful with multi-worker Gunicorn changes: in-memory download state is per worker.
- Prefer small, focused changes over broad refactors.
