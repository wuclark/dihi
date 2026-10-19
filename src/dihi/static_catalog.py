"""Generate a self-contained offline browser for the DIHI media catalog."""
from __future__ import annotations

import argparse
import datetime
import html
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

import catalog


ROOT_NAMES = {
    "merged": "media-strict",
    "legacy": "media-legacy",
    "bestfallback": "media-fallback",
}


def _media_url(source_root: str, channel: str, video: str, name: str) -> str:
    root = ROOT_NAMES.get(source_root, source_root)
    return "/".join(quote(str(part), safe="") for part in ("..", root, channel, video, name))


def _cards(database: Path) -> list[dict]:
    cards, _total = catalog.library_cards(database, page=None, sort="date-desc")
    files_by_video: dict[str, list[dict]] = {}
    for item in catalog.library_files(database):
        if isinstance(item, dict):
            files_by_video.setdefault(str(item.get("video_id", "")), []).append(item)
    for card in cards:
        videos, audios, thumbnail = [], [], None
        for item in files_by_video.get(str(card.get("video_id", "")), []):
            if not isinstance(item, dict):
                continue
            item["relative_url"] = _media_url(
                str(item.get("source_root", card.get("source_root", "merged"))),
                str(card.get("channel_id", "")), str(card.get("video_id", "")),
                str(item.get("name", "")),
            )
            if item.get("kind") == "video":
                videos.append(item)
            elif item.get("kind") == "audio":
                audios.append(item)
            elif item.get("kind") == "thumbnail" and thumbnail is None:
                thumbnail = item
        card["video_files"], card["audio_files"], card["thumbnail"] = videos, audios, thumbnail
    return cards


def _playlists(database: Path) -> list[dict]:
    with sqlite3.connect(database) as db:
        rows = db.execute(
            """SELECT p.playlist_id, p.title, p.webpage_url, pv.video_id,
                      pv.playlist_index, pv.title
                 FROM playlists p LEFT JOIN playlist_videos pv
                   ON pv.playlist_id = p.playlist_id
                 ORDER BY lower(p.title), p.playlist_id, pv.playlist_index"""
        ).fetchall()
    result: dict[str, dict] = {}
    for playlist_id, title, webpage_url, video_id, index, member_title in rows:
        playlist = result.setdefault(str(playlist_id), {
            "playlist_id": str(playlist_id), "title": title or playlist_id,
            "webpage_url": webpage_url, "members": [],
        })
        if video_id:
            playlist["members"].append({
                "video_id": str(video_id), "title": member_title or video_id,
                "playlist_index": index,
            })
    return list(result.values())


def _html(payload: dict) -> str:
    # Embedding the JSON keeps the export functional under file:// without fetch/CORS.
    embedded = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DIHI Offline Catalog</title><link rel="stylesheet" href="styles.css"></head>
<body data-catalog="{embedded}"><main><header><h1>DIHI Offline Catalog</h1><p id="summary"></p>
<input id="search" type="search" placeholder="Search title, artist, uploader, or ID"></header>
<section><h2>Playlists</h2><div id="playlists"></div></section>
<section><h2>Library</h2><div id="cards" class="cards"></div></section></main>
<script>window.CATALOG=JSON.parse(document.body.dataset.catalog)</script><script src="app.js"></script></body></html>'''


CSS = """body{font:16px system-ui,sans-serif;background:#f5f5f5;color:#222;margin:0}
main{max-width:1200px;margin:auto;padding:24px}header{position:sticky;top:0;background:#f5f5f5;padding-bottom:12px;z-index:1}
input{width:min(600px,100%);padding:10px;font-size:16px}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
article,.playlist{background:white;border:1px solid #ddd;border-radius:8px;padding:12px;box-shadow:0 1px 3px #0001}
article img{width:100%;aspect-ratio:16/9;object-fit:cover;background:#ddd}h1,h2{margin-bottom:6px}
audio,video{width:100%;margin-top:8px}.meta{color:#666;font-size:.9em}.files{margin-top:8px;display:flex;gap:8px;flex-wrap:wrap}
a{color:#0645ad}.playlist{margin:6px 0}.playlist summary{cursor:pointer}.missing{color:#888}.empty{color:#666}"""


JS = r'''const data=window.CATALOG;
const downloaded=new Set(data.cards.map(x=>x.video_id));
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const files=(items,exts)=>(items||[]).filter(x=>exts.includes(x.extension));
function render(q=''){
  q=q.toLowerCase().trim();
  const cards=data.cards.filter(x=>!q||[x.title,x.video_id,x.channel_id,x.artist,x.uploader,x.track].some(v=>String(v||'').toLowerCase().includes(q)));
  document.querySelector('#summary').textContent=`${cards.length} of ${data.cards.length} videos · generated ${data.generated_at}`;
  document.querySelector('#cards').innerHTML=cards.map(x=>{
    const image=x.thumbnail?`<img loading="lazy" src="${x.thumbnail.relative_url}" alt="">`:'';
    const vids=files(x.video_files,['mp4','webm','mkv']), auds=files(x.audio_files,['m4a','mp3','opus','webm']);
    const video=vids.length?`<video controls preload="metadata" src="${vids[0].relative_url}"></video>`:'';
    const audio=auds.length?`<audio controls preload="none" src="${auds[0].relative_url}"></audio>`:'';
    const links=[...vids,...auds].map(f=>`<a href="${f.relative_url}" download>${esc(f.name)}</a>`).join(' ');
    return `<article id="${esc(x.video_id)}">${image}<h3>${esc(x.title)}</h3><div class="meta">${esc(x.artist||x.uploader)} · ${esc(x.date||'')}</div>${video}${audio}<div class="files">${links}</div><div class="meta">${esc(x.video_id)}</div></article>`;
  }).join('')||'<p class="empty">No matching videos.</p>';
}
document.querySelector('#playlists').innerHTML=data.playlists.map(p=>{const available=p.members.filter(m=>downloaded.has(m.video_id)).length;return `<details class="playlist"><summary>${esc(p.title)} (${available}/${p.members.length} downloaded)</summary>${p.members.map(m=>downloaded.has(m.video_id)?`<a href="#${esc(m.video_id)}">${esc(m.playlist_index||'')} ${esc(m.title)} · Downloaded</a>`:`<span class="missing">${esc(m.playlist_index||'')} ${esc(m.title)} · Missing</span>`).join('<br>')}</details>`}).join('')||'<p class="empty">No playlists.</p>';
document.querySelector('#search').addEventListener('input',e=>render(e.target.value));render();'''


def generate(data_root: Path, output: Path | None = None) -> Path:
    data_root = data_root.expanduser().resolve()
    database = data_root / "media-catalog.db"
    if not database.is_file():
        raise FileNotFoundError(f"catalog database not found: {database}; run `make data` and refresh the catalog first")
    output = (output or data_root / "static-catalog").expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cards": _cards(database), "playlists": _playlists(database),
    }
    (output / "index.html").write_text(_html(payload), encoding="utf-8")
    (output / "catalog.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (output / "app.js").write_text(JS, encoding="utf-8")
    (output / "styles.css").write_text(CSS, encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the offline DIHI catalog browser")
    parser.add_argument("--data-root", type=Path, default=Path("./data"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(generate(args.data_root, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
