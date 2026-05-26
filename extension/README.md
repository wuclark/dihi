# YouTube ID Server Checker (Edge Extension, MV3)

Checks the current YouTube video ID against:

- `GET /api/youtube/<id>` → `{ "result": true|false }`

When a video is already archived, the extension can resolve the local server copy and open the server UI at `/?play=<id>&autoplay=1`:

- `GET /api/media/resolve/<id>` → `{ "result": true, "video": { "player_url": "/media/..." } }`

If result is **false** (badge **NO**), you can trigger:

- `POST /api/youtube/get/<id>`

The extension also tracks per-video YouTube visits in browser-local storage. If auto-download is enabled, a missing video is downloaded automatically after it reaches the configured visit threshold, and a notification is shown when that automatic request starts. Before posting a download request, it resolves local media again and skips the request if the video is already downloaded. Counts are reset only after the video is found in the archive, so a still-missing video that remains at or above the threshold is requested again on a later visit.

While downloading, the badge shows **DL** and it polls:

- `GET /api/youtube/status/<id>` → `{ "downloading": true|false }`

When the server reports `downloading:false`, the extension shows a **notification**.

## Install (Developer Mode)

1. Open Edge → `edge://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select this folder (`extension/`)

## Options

Open the extension’s **Options** page to set:

- Server Origin
- Timeout
- Debounce
- Auto-download missing videos
- Auto-download visit threshold
- Open archived videos from the server instead of YouTube

## Folder Layout

```
extension/
  manifest.json
  service_worker.js
  popup.html
  popup.js
  options.html
  options.js
  icons/
    icon16.png
    icon32.png
    icon48.png
    icon128.png
  README.md
```
