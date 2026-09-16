# YouTube ID Server Checker (Edge Extension, MV3)

Checks the current YouTube video ID against:

- `GET /api/youtube/<id>` → `{ "result": true|false }`

When a video is already archived, the extension can resolve the local server copy and open the server UI at `/video/<id>`:

- `GET /api/media/resolve/<id>` → `{ "result": true, "video": { "player_url": "/media/..." } }`

If result is **false** (badge **NO**), you can trigger:

- `POST /api/youtube/get/<id>`

The extension also tracks per-video YouTube visits in browser-local storage. If auto-download is enabled, a missing video is downloaded automatically after it reaches the configured visit threshold, and a notification is shown when that automatic request starts. Before posting a download request, it resolves local media again and skips the request if the video is already downloaded. Counts are reset only after the video is found in the archive, so a still-missing video that remains at or above the threshold is requested again on a later visit.

While downloading, the badge shows **DL** and it polls:

- `GET /api/youtube/status/<id>` → `{ "downloading": true|false }`

When the server reports `downloading:false`, the extension shows a **notification**.

If the server default download mode is queue-only, the extension accepts the `queue_id` returned by `POST /api/youtube/get/<id>`, shows a **Q** badge while the item is pending, and follows `/api/queue` until it starts or finishes.

## Install (Developer Mode)

1. Open Edge → `edge://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select this folder (`extension/`)

The browser does not allow an extension to be installed just by opening a
`http://localhost` page. For local development, **Load unpacked** is the
supported install method. After loading it, open the extension's Options page
and set **Server Origin** to `http://localhost:5000` when the dihi server is
running on the same computer. The manifest already permits localhost HTTP
origins.

The running dihi server also provides the current extension as
`http://localhost:5000/extension.zip`. Download and extract that ZIP, then
choose the extracted folder with **Load unpacked**.

## Options

Open the extension’s **Options** page to set:

- Server Origin
- Timeout
- Debounce
- Auto-download missing videos
- Auto-download visit threshold
- Open archived videos from the server instead of YouTube
- Archived playback mode: preflight redirect, in-page player replacement, or ask

The archive playback option can be disabled when you want YouTube pages to
remain the default. The options page also includes a direct link to the
configured dihi server. Dihi's download status page exposes YouTube URLs as
copy-only buttons, so opening YouTube is an explicit address-bar navigation
without a referrer from the dihi page.

In-page replacement keeps the YouTube page around the local player but still
loads YouTube's page resources. The local player starts muted because browsers
block audible autoplay without a user gesture or prior site permission; click
the player's unmute control to enable sound.

## Folder Layout

```
extension/
  manifest.json
  service_worker.js
  content_player.js
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
