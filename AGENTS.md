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

Keep `handoff.txt` current while work is in progress or is being handed between
agents. Update it whenever the active implementation, verification results,
known blockers, runtime/storage situation, or next steps change. The handoff
must identify the current state accurately enough for another agent to resume
without re-discovering the work; do not commit cookies, downloaded media, or
other runtime data into it.

Before finishing a substantial change, quickly check whether these sections still match the code:

- active entrypoints
- run/test commands
- API and UI routes
- download format logic
- output file layout
- Docker/Gunicorn config
- known caveats

## Active Entrypoints

- Server: `src/dihi/app3.py` (uses `DIHI_DATA_DIR` for host runs)
- Web UI templates: `src/dihi/templates/index.html`, `src/dihi/templates/tags.html`
- Library frontend module: `src/dihi/static/js/library.js`
- Downloader: `src/dihi/getvidyt.py`
- CLI: `src/dihi/cli.py`
- Offline catalog exporter: `src/dihi/static_catalog.py`
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
make usb-mount USB_DRIVE=D:
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

`make docker-up` verifies the configured data root before initializing bind-mount data files, then builds and starts the dihi server and PO Token provider. For a Windows USB data root, `make usb-mount USB_DRIVE=D:` mounts it at `/mnt/usbdocker1`; `docker-up` fails with a recovery command if that mount is unavailable. `make docker-down` stops the Compose services; `make docker-logs` follows their logs. `make git-add` intentionally excludes local runtime data: everything under `data/`.

`make data-size` reports total and per-directory runtime data usage. After
stopping Compose, `make data-migrate DEST_DATA_DIR=/mnt/usbdocker1/docker/dihi/data` copies
the configured data root to a new location without deleting the source. The
migration uses Windows/DrvFs-compatible copy options and can be rerun after an
interruption; it does not preserve Linux ownership, permissions, or timestamps.
Set
`DIHI_DATA_DIR` in `.env` to the destination only after verifying the copy.
If a Windows USB drive is assigned `D:` but is not auto-mounted by WSL2, mount
it at the neutral `/mnt/usbdocker1` path:

```bash
sudo mkdir -p /mnt/usbdocker1
sudo mount -t drvfs D: /mnt/usbdocker1
ls -la /mnt/usbdocker1
```

Replace `D:` with the assigned Windows drive letter when necessary. Use
`DIHI_DATA_DIR=/mnt/usbdocker1/docker/dihi/data` for the Docker data root.

`make test-docker` starts the Compose stack and runs opt-in smoke tests against
the live HTTP service. The normal `venv/bin/pytest` suite remains Docker-free.

`make catalog-static` generates `DIHI_DATA_DIR/static-catalog`, a self-contained
offline catalog browser that references the existing media tree without copying
media. `make catalog-static-serve` regenerates it and serves it on localhost
port 8765; `STATIC_CATALOG_PORT` can change the port. The export does not
require Flask, Docker, or network access after generation.

## Server Routes

Core UI/media routes:

- `GET /` renders the media library UI
- Main-library cards open the inline player and start playback with sound from the card click; direct links use `/?play=<video_id>&autoplay=1` (legacy `?video=` links remain accepted) and start muted because browsers block audible autoplay. Muted autostarts show a 🔊 Tap for sound button that restores sound with one click. The inline player has ⏮ Prev / ⏭ Next controls with a position indicator (e.g. `3/12`) that step through the playable list; track end auto-advances only in Play All mode.
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
- `GET /library-tests` renders interactive library search, sort, and playlist API/UI tests
- `GET /api-docs` renders the available API endpoint reference
- `GET /sitemap` renders the site map
- `GET /cleanup` renders the dry-run cleanup report
- `GET /status` renders disk and cookie diagnostics
- `GET /api/media/library` lists library cards from the SQLite catalog (supports `?page=&per_page=&sort=&q=`; default returns all cards with `total`; `q` matches title, video ID, channel ID, artist, uploader, and track). Cards carry `artist` (true music metadata when present, else uploader/channel name), `uploader`, and `track` fields; `sort=artist` orders by artist name. The `track` column is added by migration on old databases.
- `GET /api/media/library/files` lists every indexed library file with a link from the catalog
- `GET /api/media/library/files.txt` exports every indexed library file link as text from the catalog
- `GET /api/media/library/youtube.txt` exports all video and playlist YouTube links from the catalog
- `GET /api/media/library/ids.txt` exports all video and playlist IDs from the catalog
- `GET /api/media/details/<channel_id>/<video_id>` returns per-video files and metadata
- `GET /api/media/resolve/<video_id>` returns the archived media record and preferred playable URL for one YouTube ID
- `GET /api/media/tags` returns tag counts and tag-grouped videos
- `GET /api/media/playlists` lists downloaded playlists with total, locally available, and missing member counts; incomplete playlists sort first
- `GET /api/media/playlists/<playlist_id>` returns a named playlist and its ordered local video members
- `POST /api/media/playlists/<playlist_id>/refresh` refreshes membership without deleting local media; preflight ignores the download archive so previously downloaded members stay listed; if YouTube returns an empty/unviewable playlist, it repairs membership from downloaded video metadata when possible
- `POST /api/media/playlists/<playlist_id>/finish` queues each incomplete member independently and skips complete members
- `POST /api/media/playlists/snapshot` saves browser-captured autogenerated-playlist members as a timestamped local snapshot and can queue members individually
- `DELETE /api/media/playlists/<playlist_id>` removes a local `SNAP_` snapshot descriptor without deleting downloaded media
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
- The Tools page includes startup timing history, a confirmation-free filesystem rescan button that refreshes catalog paths and links without deleting media.
- `POST /api/media/cleanup/verify` starts a full media verification pass cached by file MD5
- `POST /api/media/cleanup/retry-missing` queues incomplete media while skipping permanent failures and complete files
- Catalog rows with missing metadata, formats, thumbnails, descriptions, or subtitles/lyrics expose a “Retry missing files” action; sidecar retries bypass the download archive when needed.
- `GET /api/media/wordcloud` returns description word frequencies, filterable by tag or playlist
- `GET /api/media/failures` lists recorded failed download attempts and reasons
- `GET /api/media/download-history` lists persistent completed and failed download attempts
- `GET /api/system/status` reports disk capacity and safe cookie-file diagnostics without cookie values
- `GET /api/system/cache-status` reports cache coverage and refresh state for expensive endpoint groups
- `GET /api/system/startup-history` returns persisted cold/full-scan and warm/cache-hit startup timings shown on `/status`
- `GET /api/downloads/status` returns active video/playlist downloads, recent completed/failed results, queue summary fields including `Queue Empty` when idle, and yt-dlp progress details (`phase`, `percent`, `filename`, and recent `logs`)
- `GET /api/queue` lists persistent pending, running, completed, failed, and cancelled queue items
- `POST /api/queue` adds a video or playlist to the queue; the default is immediate start, configurable through `/api/settings`
- `POST /api/queue/<item_id>/start` starts a paused/scheduled queue item immediately
- `POST /api/queue/<item_id>/pause` requests a cooperative pause for a running item; video jobs pause after the current file and playlist jobs after the current child video
- `POST /api/queue/<item_id>/stop` immediately persists a running queue item as paused, then stops active yt-dlp work at the next progress boundary; the live dashboard reports `stopping` during that handoff
- `POST /api/queue/<item_id>/cancel` cancels a pending/paused item or stops a running item and removes it from the queue
- Queue listing and scheduling repair old rows that stored playlist IDs as video jobs.
- `POST /api/queue/start-all` starts all paused queue items
- `POST /api/queue/clear` removes pending and paused queue items without interrupting active downloads
- `POST /api/queue/cancel-all` cancels all pending and paused queue items
- `GET/POST /api/settings` reads or updates the default add behavior (`immediate` or `queue`) and concurrent video/playlist limits
- `GET /media/<path>` serves downloaded files with conditional/range-capable responses
- `GET /media-legacy/<path>` serves files from the legacy `data/media-legacy` tree
- `GET /media-fallback/<path>` serves fallback-library files with conditional responses

The rebuildable SQLite catalog scans `data/media-strict/`,
`data/media-legacy/`, and `data/media-fallback/`, retaining source-root and per-file format/subtitle
details. It also stores playlist names and video memberships from each `.info.json`; a video can belong to multiple playlists. The library, files/exports, playlist-detail, tags, and resolve endpoints are served from this catalog (with a live-scan fallback during the first-startup window). Download failures are classified and persisted as retry information. The catalog endpoint initializes its schema before reading so the UI can continue serving while the background filesystem scan is running. Playlist preflight waits for that startup scan's SQLite write lock when persisting playlist membership.
Rebuildability: videos, tags, files, formats, archive status, and playlists
(including `data/playlists/*.info.json` descriptors) are fully rebuilt by
deleting `data/media-catalog.db` and rescanning. `app_settings` also survives
via `data/settings.json` (single-object JSON, `DIHI_SETTINGS_FILE` override):
missing keys are seeded from the file after a rescan, every settings write
backs up to it, and the DB wins on conflict. Not rebuildable and DB-authoritative:
`download_queue` (pending/scheduled targets) and `download_attempts` history
(reasons/errors/timestamps).
The planned indexed artist/album pages and safe rollback are documented in
[`plan.media-catalog.md`](plan.media-catalog.md). It is not required by the
current catalog-backed library; it remains a rebuildable runtime index while
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

Docker bind mounts the complete `${DIHI_DATA_DIR}` tree (default `./data`) to
`/app/data`. This keeps archive files, cookies, strict/legacy/fallback media,
playlists, SQLite catalog, queue/history, settings backup, and verification
cache on the host data root. Docker images, build cache, and container logs
remain outside that tree.

Downloads are stored under:

```text
data/media-strict/
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

The generated offline browser is stored under
`DIHI_DATA_DIR/static-catalog/` and references the existing media trees with
relative links; it does not copy media files.

The exact set varies by source video and available formats. The `<channel_id>/<video_id>/` directory structure is intentional because those IDs are stable across title/channel renames. The catalog also stores playlist membership independently, so preflighted playlist entries link existing media without downloading it again. Preflight-only playlists are saved as rebuildable `data/playlists/<playlist_id>.info.json` descriptors and imported when the SQLite catalog is rebuilt.

The default `.out.m4a` is the separately requested AAC audio download. `--audio-meta` can make additional clean, tagged copies from kept raw audio sidecars, such as `.out.f251.webm` to `.out.webm`.

`data/media-strict/` is the strict output tree and uses `data/archive.txt`. Failed strict downloads retry into `data/media-fallback/` with its own `archive.txt`; the fallback archive does not mark a strict download complete. `keepvideo: True` preserves raw `.f<id>.<ext>` streams alongside the merged MKV, so disk use includes duplicate media data.

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
- A video is marked complete only when both playable video and audio files exist; the completion check reads the live strict/legacy directory before the catalog so freshly downloaded outputs are recognized, then restores the archive entry after successful archive-bypassing retries. If an archive entry exists without both playable outputs, retries remove that ID from the archive and bypass it so yt-dlp does not skip the repair. Incomplete attempts are persisted as failures with a reason and remain retryable from the catalog, including browser-cookie authentication.
- Optional yt-dlp postprocessor errors do not trigger a fallback retry when the canonical playable video/audio pair already exists; the worker likewise treats that pair as authoritative completion. This avoids duplicate lower-quality downloads when, for example, thumbnail embedding rejects a kept WebM sidecar.
- The cleanup report uses `ffprobe` and FFmpeg stream-copy remux checks; the Docker image installs both as part of the `ffmpeg` package. Results are cached per source folder/container during each report.
- The word cloud keeps lyric vocabulary but filters common URL terms and description credit/promotion boilerplate (such as video, lyrics, director, producer, and subscribe). `/wordcloud` generates only when its Generate button is pressed and shows a spinner/progress state while descriptions are scanned and the layout is arranged.
- Server-triggered video and playlist downloads call `download_youtube(..., audio_meta=True)`. Playlist downloads preflight entries and invoke the downloader per child video, so fallback is isolated to failed children and successful strict children are not re-downloaded into `data/media-fallback/`.
- Deno/`yt-dlp-ejs` is used for YouTube JS challenge solving unless `no_js=True`.
- yt-dlp must be at least 2026.08.19; older releases can return HTTP 403 for `android_vr` video streams. `make setup` refreshes the pinned version after `requirements.txt` changes.
- The `bgutil-ytdlp-pot-provider` plugin supplies per-video GVS PO Tokens for the `mweb` client. `make pot-provider` starts its Docker service, and `make <video ID or URL>` starts it automatically. Compose publishes its port only on `127.0.0.1:4416`; the app container uses `DIHI_PO_TOKEN_PROVIDER_URL=http://bgutil-provider:4416`. The host default is `http://127.0.0.1:4416`.
- Host workflow: `make setup`, `make dev-install`, then `make <video ID or URL>`. `make setup` installs the Python plugin but does not start its Docker service. Direct `venv/bin/dihi download` uses `DIHI_DATA_DIR` when exported (otherwise `./data`); `make <video ID or URL>` and `make run` pass the configured Make data root automatically. Direct downloads and `make run` need `make pot-provider` first if they will download. `docker compose up -d --build` starts the provider with the app through `depends_on`; run `make data` first for bind-mount files.

## UI Notes

The web UI is part of `app3.py`, not a separate server yet. It lists the archive (strict `data/media-strict/` first, then legacy `data/media-legacy/`, then `data/media-fallback/` playable entries last), batches thumbnail rendering, supports video/audio playback, exposes VLC URLs, loads per-video details lazily, and includes `/queue` for persistent manual/scheduled downloads. The default add behavior is immediate; changing the queue setting holds new items until manually started. An immediate video request, including a per-song Download button on the playlist page, promotes an existing paused row to pending. Jobs interrupted by a server restart are recovered as paused queue items and do not auto-resume; if an interrupted duplicate collides with an existing paused row, recovery keeps the existing paused item. Running queue rows can request a cooperative pause: a single video pauses after its current file, while a playlist pauses after its current child video and remains resumable. The queue page's playlist batch tool accepts bare IDs, `playlist <ID>` lines, URLs, and CSV-style watch rows; its preparation button creates missing playlist descriptor JSON files, skips valid existing descriptors, and reports each playlist's progress in the output box. `/playlists` provides named playlist membership, ordered video links, per-video download buttons, browser play-all, and separate VLC video/audio-only M3U files; its Refresh playlist action explicitly re-fetches membership. `/downloads` shows active/recent download status, persistent attempt history, failed-download retry actions, and a remaining queue output. Playlist progress groups each item's video, audio, and metadata files beneath that item's title. `/downloaded` shows the same persistent completed/failed attempt history in a scrollable panel. `/video/<video_id>` shows the saved description collapsed by default, full metadata, every indexed file with size/type, playable video/audio links, and the VLC media URL. The main library restores active/recent queue entries from the server after refresh; its search and sort controls query the catalog server-side so large archives do not need to load and filter the entire catalog in the browser. Library cards show an artist line (true `artist`/`track` music metadata when present, else the uploader/channel name); the sort control includes Artist A–Z and search matches artist, uploader, and track as well as title and IDs. Playlist rows expose child video links and `playlist_index/total` progress as yt-dlp encounters each entry. Queue state changes are also written to the server log, including `Queue Empty` when all active downloads finish.
All non-library pages use the shared `templates/_header.html` navigation. The library header keeps primary library/download links; `/tools` is the hub for the catalog, tags, word cloud, tag cloud, Edge extension, status, downloaded-history, and library search/sort test tools. `/library-tests` is the dedicated test page with playlist selection by name, generated UI/API links, expected results, and live JSON output. Saved playlist cards show locally available versus total members, such as `9/10 videos`, and incomplete playlists sort first; empty metadata records show `0/0 · no members captured` rather than complete. The saved-playlists page hides `RD...` autogenerated Mix records by default with a browser-persistent toggle; local `SNAP_` snapshots can be removed without deleting downloaded media.

The browser extension (version 1.9.0) tracks per-video YouTube visit counts in `chrome.storage.local`. When enabled, it auto-downloads missing videos after the configured visit threshold and notifies when the automatic request starts. It calls `/api/media/resolve/<video_id>` before posting a download request and skips the request if local media already exists. Resolve is strict-only (ignores `data/media-fallback/`), so fallback copies keep getting upgraded to strict downloads. Counts reset only once the video is found in the archive, so still-missing videos at or above the threshold are requested again on later visits. On YouTube `RD...` Mix watch pages, the popup can snapshot the loaded Mix members into a timestamped local playlist and optionally queue each video individually because yt-dlp cannot enumerate that autogenerated playlist type. Archived playback can use a preflight redirect, an in-page local player replacement, or an ask prompt. In-page replacement keeps YouTube around the player but still loads YouTube page resources; local autoplay starts muted because audible autoplay requires browser permission or a user gesture. The dihi downloads UI exposes source YouTube URLs as copy-only controls instead of direct links.

Browser playback of `.mkv` is inconsistent across browsers and codecs. Keep the VLC URL path available when changing playback behavior.

## Tests

Run:

```bash
venv/bin/pytest
```

The current suite is pure unit tests: no network, no real yt-dlp download, no ffmpeg integration, and no browser automation. `make test-docker` additionally runs opt-in smoke tests against the live Compose HTTP service.

The default `/` view is the dark Library browser. It has a paste-and-go
YouTube URL/ID download control that starts an immediate queue item. It loads
rich catalog rows
from `/api/media/library`, merges tags from `/api/media/tags`, supports URL
search/artist/sort/view state, grid/list cards, local queue persistence, and a
fixed media player. The live download dashboard remains at `/downloads`; the
dense catalog/status table remains at `/catalog` and is linked separately as
Catalog / admin table. Its rebuild button remains on that admin page.
The shared sidebar is responsive, keeps all saved playlists reachable in a
scrollable playlist section, and sorts the playlist links alphabetically while
preserving the existing routes.
Library grid cards use compact responsive tiles with visible tag chips. Clicking
a thumbnail opens the in-page player with native video controls and keeps the
fixed transport controls available; fullscreen is only entered through the
explicit expand button.
The Library script is loaded with a manually incremented query-string
cache-buster (currently `library.js?v=9`) so browsers fetch frontend changes
instead of reusing a stale cached script. Bump it whenever `library.js` changes;
automatic content-hash asset versioning remains a future improvement. The
player attaches indexed VTT/SRT files as native captions tracks and defaults
to the first available track. The saved-playlists page supports A–Z and
updated-date sorting; the root Library also provides a playlist filter and
the same playlist sort choices. Explicit Library fullscreen includes Previous,
Play/Pause, and Next controls that appear when the pointer reaches the bottom.
Fullscreen removes the player-panel padding and uses a viewport-sized black
stage with the video contained inside it, so any remaining black area is only
aspect-ratio letterboxing rather than a page border.
The fullscreen title and Close player control are hidden by default and appear
when the pointer reaches the top edge, matching the bottom transport controls.
Library playback actions explicitly play all shown/filtered files or play them
in random order; the Up next panel also provides Play queue and Random queue.

## Roadmap / TODO

- **Library and playback follow-up (current UI roadmap)**
  1. **Progressive Library loading** — render the first catalog page immediately, then load additional pages/tags in the background; preserve search across large archives.
  2. **Persistent player across navigation** — keep the media element alive while moving between Library, playlists, artists, tags, and other browsing pages.
  3. **Proper fullscreen player** — replace the native video-only fullscreen behavior with a DIHI overlay that keeps previous, next, queue, seek, volume, and exit controls available.
  4. **Accurate Library status details** — expose requested/downloaded/fallback formats and duration through a lightweight catalog response without USB filesystem scans.
  5. **Playlist picker dialog** — add create-new-playlist behavior where supported. The Library now uses an accessible native dialog for existing saved playlists.
  6. **Frontend refactor** — split the Library module further and add frontend tests for state and playback behavior. The first extraction is complete in `src/dihi/static/js/library.js`.
  7. **Visual consistency pass** — bring Downloads, Catalog, Playlists, Tags, Queue, and Tools onto the shared dark design without removing their existing functionality.
  8. **Navigation and route audit** — keep `/downloads` as live status, `/catalog` as admin table, and verify every existing route remains reachable from the sidebar or Tools/export area.
  9. **Browser smoke tests** — test card playback, queue ordering, audio-only mode, fullscreen exit, retry actions, responsive navigation, and playlist actions in a real browser.
  10. **Cache coverage and invalidation** — cache tags, library exports, and short-lived system-size results by the validated catalog fingerprint; reduce repeated USB fingerprint walks. Tags, exports, system status caching, and a fast directory-structure fingerprint are now implemented; remaining work is broader invalidation/measurement.
  11. **Debounced catalog refreshes** — coalesce post-download filesystem rescans and expose refresh progress so a playlist batch does not trigger one full scan per child. Post-download refreshes are now coalesced; progress reporting remains.
  12. **Timestamp all cache and endpoint operations** — record request/start and completion timestamps, duration, last success, last failure, refresh source, and current phase for every row in the Status cache/endpoint table and relevant download/catalog logs.

- **Recommended implementation order**
  1. Progressive Library loading and performance measurements.
  2. Frontend JavaScript refactor and shared player state.
  3. Proper fullscreen player overlay and keyboard/accessibility tests.
  4. Playlist picker dialog and accurate lightweight status details.
  5. Visual consistency pass, route audit, and browser smoke-test coverage.

- Keep the browser extension mirrored with the active site flows and API endpoints. When queue, playlist, playback, settings, or response-shape behavior changes, update the extension code, extension README, and extension version together, then test the extension against the documented endpoints.
- Serve the remaining scan-backed callers (`/api/media/tags` fallback already catalog-first; per-video details still reads the live directory lazily) from the SQLite catalog if they show up in profiling, and switch the web UI from client-side filtering to `/api/media/library?q=` on huge archives.
- Extend the Status cache/endpoint table to cover every expensive route, with persisted last-run timings and safe scoped refresh actions.
- Validate the WSL2 Windows USB workflow end to end: copy existing `data/` contents to the USB path, run `make data` with `.env` set, verify all Compose mounts, and document recovery if the drive is disconnected.
- Measure USB/DrvFs performance for large media trees and decide whether media belongs on USB while the SQLite catalog, archive, and queue state remain on the native WSL filesystem.
- Document a separate Docker Desktop disk-image relocation if Docker images/build cache—not DIHI media—are consuming the Windows C: drive.

## Coding Caveats

- Playlist cards show their saved/updated date, retain every captured member
  including unavailable videos, queue each missing member independently
  without waiting for playlist completion, and time out stalled member-detail
  requests with a retryable message. Playlist detail reads the SQLite catalog
  directly and must not trigger a full media-tree scan; saved descriptor JSON
  is preferred, and playlist listing/detail must return from descriptor/live-
  media data even while the catalog writer is locked.
- Keep `app3.py` as the active server unless explicitly asked to split the UI/API.
- Do not commit cookies or downloaded media.
- Do not stage or commit local runtime data: everything under `data/` is git-ignored.
- Preserve `/api/youtube/*` compatibility for the extension.
- Be careful with multi-worker Gunicorn changes: in-memory download state is per worker.
- SQLite catalog scans can hold the database write lock for a while; queue
  reads/writes use a long busy timeout so the scheduler waits rather than
  repeatedly logging transient `database is locked` failures.
- Catalog refreshes and download-attempt writes use long SQLite busy timeouts,
  and the expensive playlist-index build is serialized per worker so concurrent
  playlist page requests do not rebuild 303 descriptors simultaneously.
- Downloads failure/history endpoints must return JSON even when the catalog
  is busy; the UI should handle non-JSON responses without exposing a parser
  exception. Download-history reads must not run schema migrations on the
  read path.
- Downloads dashboard queue totals include persistent running, pending, and
  paused queue items; concurrency counters remain active-worker-only.
- Prefer small, focused changes over broad refactors.
