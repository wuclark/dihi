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

## Common Commands

```bash
make setup
make dev-install
venv/bin/pytest
python src/dihi/app3.py
docker compose up -d --build
make git-add
make git-commit-push MSG="Describe the change"
```

Current `make run` invokes `main.py`, which does not exist. Use `python src/dihi/app3.py` unless the Makefile is fixed.

`make git-add` intentionally excludes local runtime data paths: `data/**`, root `archive.txt`, root `cookies.txt`, and `audio/**`.

## Server Routes

Core UI/media routes:

- `GET /` renders the media library UI
- `GET /tags` renders the tag browser UI
- `GET /api/media/library` lists library cards
- `GET /api/media/details/<channel_id>/<video_id>` returns per-video files and metadata
- `GET /api/media/tags` returns tag counts and tag-grouped videos
- `GET /media/<path>` serves downloaded files with conditional/range-capable responses

Archive/download API routes:

- `GET /health`
- `GET /api/youtube/<video_id>`
- `POST /api/youtube/get/<video_id>`
- `GET /api/youtube/status/<video_id>`
- `POST /api/youtube/playlist/get/<playlist_id>`
- `GET /api/youtube/playlist/status/<playlist_id>`

The extension depends on the `/api/youtube/*` endpoints. Keep their response shapes stable unless updating the extension in the same change.

## Data And Output Layout

Archive files use yt-dlp format:

```text
youtube <video_id>
```

Docker bind mounts:

- `./data/archive.txt` -> `/app/archive.txt`
- `./data/cookies.txt` -> `/app/cookies.txt`
- `./data/merged` -> `/app/merged`

Downloads are stored under:

```text
merged/
└── <channel_id>/
    └── <video_id>/
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.mkv
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.f140.m4a
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.m4a
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.info.json
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.formats.json
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.description
        ├── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.png
        └── <channel_id>.<video_id>.<date>.<title> [<video_id>].out.en.vtt
```

The exact set varies by source video and available formats. The `<channel_id>/<video_id>/` directory structure is intentional because those IDs are stable across title/channel renames.

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

- Thumbnails are converted to PNG.
- Metadata, chapters, thumbnails, subtitles, descriptions, info JSON, and format manifests are written when available.
- `AudioMetadataPostProcessor` can create clean audio copies with embedded metadata.
- Server-triggered video and playlist downloads call `download_youtube(..., audio_meta=True)`.
- Deno/`yt-dlp-ejs` is used for YouTube JS challenge solving unless `no_js=True`.

## UI Notes

The web UI is part of `app3.py`, not a separate server yet. It lists the archive, batches thumbnail rendering, supports video/audio playback, exposes VLC URLs, and loads per-video details lazily.

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
