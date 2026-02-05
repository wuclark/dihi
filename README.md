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

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/api/youtube/<id>` | GET | Check if video is archived |
| `/api/youtube/get/<id>` | POST | Trigger video download |
| `/api/youtube/status/<id>` | GET | Check download progress |

### Example Usage

```bash
# Check if video is archived
curl http://localhost:5000/api/youtube/dQw4w9WgXcQ

# Trigger download
curl -X POST http://localhost:5000/api/youtube/get/dQw4w9WgXcQ

# Check download status
curl http://localhost:5000/api/youtube/status/dQw4w9WgXcQ
```

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
