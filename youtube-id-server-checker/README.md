# YouTube ID Server Checker (Edge Extension, MV3)

Checks the current YouTube video ID against:

- `GET /api/youtube/<id>` → `{ "result": true|false }`

If result is **false** (badge **NO**), you can trigger:

- `POST /api/youtube/get/<id>`

While downloading, the badge shows **DL** and it polls:

- `GET /api/youtube/status/<id>` → `{ "downloading": true|false }`

When the server reports `downloading:false`, the extension shows a **notification**.

## Install (Developer Mode)

1. Open Edge → `edge://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select this folder (`youtube-id-server-checker/`)

## Options

Open the extension’s **Options** page to set:

- Server Origin
- Timeout
- Debounce

## Folder Layout

```
youtube-id-server-checker/
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
