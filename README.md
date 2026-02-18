# dihi

A YouTube video archive management system with a REST API and browser extension.

## Features

- **YouTube Video Archiving**: Download and archive YouTube videos using yt-dlp
- **REST API**: Check archive status and trigger downloads
- **Browser Extension**: Chrome/Edge extension shows archive status on YouTube pages
- **Docker Support**: Easy deployment with Docker Compose

## Quick Start (Docker)

1. Clone the repository and create data directory:
   ```bash
   mkdir -p data
   touch data/archive.txt data/cookies.txt
   ```

2. (Optional) Add YouTube cookies to `data/cookies.txt` in Netscape format for authenticated downloads

3. Start the container:
   ```bash
   docker-compose up -d --build
   ```

4. The API will be available at `http://localhost:5000`

## API Endpoints

| Method | Path | Rate Limit | Description |
|--------|------|------------|-------------|
| `GET` | `/health` | 30/min | Health check; reports archive existence and active downloads |
| `GET` | `/api/youtube/<id>` | 60/min | Check if a video is in the archive |
| `POST` | `/api/youtube/get/<id>` | 10/min | Trigger a video download in the background |
| `GET` | `/api/youtube/status/<id>` | 60/min | Poll video download progress |
| `POST` | `/api/youtube/playlist/get/<playlist_id>` | 5/min | Trigger a full playlist download in the background |
| `GET` | `/api/youtube/playlist/status/<playlist_id>` | 60/min | Poll playlist download progress |

Video IDs are exactly 11 characters (`[A-Za-z0-9_-]{11}`). Playlist IDs are 2–128 characters from the same alphabet.

### `GET /health`

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

Returns `{"result": false}` when the video is not in the archive.

### `POST /api/youtube/get/<id>` — trigger video download

```bash
curl -X POST http://localhost:5000/api/youtube/get/dQw4w9WgXcQ
```

```json
{
  "ok": true,
  "id": "dQw4w9WgXcQ",
  "started": true,
  "already_running": false
}
```

Returns HTTP 429 when there are already 5 concurrent downloads.

### `GET /api/youtube/status/<id>` — poll video download progress

```bash
curl http://localhost:5000/api/youtube/status/dQw4w9WgXcQ
```

While downloading:

```json
{
  "downloading": true,
  "id": "dQw4w9WgXcQ",
  "result": null,
  "in_archive": false
}
```

After completion:

```json
{
  "downloading": false,
  "id": "dQw4w9WgXcQ",
  "result": "completed",
  "in_archive": true
}
```

`result` is `"completed"`, `"failed"`, or `null`. The result is consumed on first read and expires after 5 minutes.

### `POST /api/youtube/playlist/get/<playlist_id>` — trigger playlist download

```bash
curl -X POST http://localhost:5000/api/youtube/playlist/get/PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm
```

```json
{
  "ok": true,
  "id": "PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm",
  "started": true,
  "already_running": false
}
```

Returns HTTP 429 when there are already 2 concurrent playlist downloads.

### `GET /api/youtube/playlist/status/<playlist_id>` — poll playlist download progress

```bash
curl http://localhost:5000/api/youtube/playlist/status/PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm
```

```json
{
  "downloading": false,
  "id": "PLbpi6ZahtOH6Ar_3GPy3gD_U6v-DWxvXm",
  "result": "completed"
}
```

`result` is `"completed"`, `"failed"`, or `null`.

## Browser Extension

The extension displays badge indicators on YouTube pages:

| Badge | Meaning |
|-------|---------|
| Green | Video is archived |
| Red | Video not in archive |
| Yellow | Download in progress |
| Gray | Cannot connect to API |

### Installation

1. Open Chrome/Edge and go to `chrome://extensions`
2. Enable "Developer mode"
3. Click "Load unpacked" and select the `extension/` folder
4. Configure the API URL in the extension popup if needed

## Local Development

### Requirements

- Python 3.12+
- Deno (for YouTube JS challenge solving)
- ffmpeg

### Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Deno
curl -fsSL https://deno.land/install.sh | sh

# Run the server
python src/dihi/app3.py
```

## Configuration

### Docker Volumes

| Volume | Description |
|--------|-------------|
| `./data/archive.txt` | Download archive (tracks downloaded videos) |
| `./data/cookies.txt` | YouTube cookies for authenticated downloads |
| `./data/merged` | Downloaded video files |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 5000 | API server port |

## Download Output

Videos are saved to `merged/<video_id>/` with:
- Video file (`.mkv`)
- Metadata (`.info.json`)
- Description (`.description`)
- Thumbnail (`.png`)
- Subtitles (if available)

## License

MIT
