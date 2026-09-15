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
make run
venv/bin/pytest
python src/dihi/app3.py
docker compose up -d --build
make git-add
make git-commit-push MSG="Describe the change"
```

`make help` works before setup and is the default Make target. `make setup` installs the pinned yt-dlp and PO Token plugin in the venv and writes `venv/.setup-complete` only after all installs succeed; a failed pip run remains retryable. `make dev-install` installs the CLI entry point. Both use venv executables directly. To activate the venv in your shell, run `source venv/bin/activate` yourself (or use `make startup` to display that command). `make <video ID or URL>` starts the Docker PO Token service before downloading unless `DIHI_PO_TOKEN_PROVIDER_URL` points to another provider. `make pot-provider` starts it explicitly for direct CLI calls or server-triggered downloads. `make run` starts the active server in `src/dihi/app3.py`.

`make docker-up` initializes bind-mount data files, then builds and starts the dihi server and PO Token provider. `make docker-down` stops the Compose services; `make docker-logs` follows their logs. `make git-add` intentionally excludes local runtime data paths: `data/**`, root `archive.txt`, root `cookies.txt`, and `audio/**`.

## Server Routes

Core UI/media routes:

- `GET /` renders the media library UI
- `GET /extension.zip` downloads the browser extension bundle for local installation
- `GET /tags` renders the tag browser UI
- `GET /downloads` renders the active/recent download status UI
- `GET /api/media/library` lists library cards
- `GET /api/media/details/<channel_id>/<video_id>` returns per-video files and metadata
- `GET /api/media/resolve/<video_id>` returns the archived media record and preferred playable URL for one YouTube ID
- `GET /api/media/tags` returns tag counts and tag-grouped videos
- `GET /api/downloads/status` returns active video/playlist downloads, recent completed/failed results, queue summary fields including `Queue Empty` when idle, and yt-dlp progress details (`phase`, `percent`, `filename`, and recent `logs`)
- `GET /media/<path>` serves downloaded files with conditional/range-capable responses

Archive/download API routes:

- `GET /health`
- `GET /api/youtube/<video_id>`
- `POST /api/youtube/get/<video_id>`
- `GET /api/youtube/status/<video_id>`
- `POST /api/youtube/playlist/get/<playlist_id>`
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
- `./data/merged` -> `/app/merged`
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

The exact set varies by source video and available formats. The `<channel_id>/<video_id>/` directory structure is intentional because those IDs are stable across title/channel renames.

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
- Server-triggered video and playlist downloads call `download_youtube(..., audio_meta=True)`.
- Deno/`yt-dlp-ejs` is used for YouTube JS challenge solving unless `no_js=True`.
- yt-dlp must be at least 2026.08.19; older releases can return HTTP 403 for `android_vr` video streams. `make setup` refreshes the pinned version after `requirements.txt` changes.
- The `bgutil-ytdlp-pot-provider` plugin supplies per-video GVS PO Tokens for the `mweb` client. `make pot-provider` starts its Docker service, and `make <video ID or URL>` starts it automatically. Compose publishes its port only on `127.0.0.1:4416`; the app container uses `DIHI_PO_TOKEN_PROVIDER_URL=http://bgutil-provider:4416`. The host default is `http://127.0.0.1:4416`.
- Host workflow: `make setup`, `make dev-install`, then `make <video ID or URL>`. `make setup` installs the Python plugin but does not start its Docker service. Direct `venv/bin/dihi download` and `make run` need `make pot-provider` first if they will download. `docker compose up -d --build` starts the provider with the app through `depends_on`; run `make data` first for bind-mount files.

## UI Notes

The web UI is part of `app3.py`, not a separate server yet. It lists the archive, batches thumbnail rendering, supports video/audio playback, exposes VLC URLs, loads per-video details lazily, and includes `/downloads` for active/recent download status with a remaining queue output. Queue state changes are also written to the server log, including `Queue Empty` when all active downloads finish.

The browser extension tracks per-video YouTube visit counts in `chrome.storage.local`. When enabled, it auto-downloads missing videos after the configured visit threshold and notifies when the automatic request starts. It calls `/api/media/resolve/<video_id>` before posting a download request and skips the request if local media already exists. Counts reset only once the video is found in the archive, so still-missing videos at or above the threshold are requested again on later visits. When archived playback redirection is enabled, archived YouTube watch pages are redirected to the server UI at `/?play=<video_id>&autoplay=1` after `/api/media/resolve/<video_id>` confirms a local media file exists.

Browser playback of `.mkv` is inconsistent across browsers and codecs. Keep the VLC URL path available when changing playback behavior.

## Tests

Run:

```bash
venv/bin/pytest
```

The current suite is pure unit tests: no network, no real yt-dlp download, no ffmpeg integration, and no browser automation.

## Coding Caveats

- Keep `app3.py` as the active server unless explicitly asked to split the UI/API.
- Do not commit cookies or downloaded media.
- Do not stage or commit local archive/runtime data such as `data/archive.txt`, root `archive.txt`, or `audio/**`.
- Preserve `/api/youtube/*` compatibility for the extension.
- Be careful with multi-worker Gunicorn changes: in-memory download state is per worker.
- Prefer small, focused changes over broad refactors.
