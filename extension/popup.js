function setStatus(text, cls) {
  const pill = document.getElementById("statusPill");
  pill.className = "pill " + (cls || "");
  pill.textContent = text;
}

async function getStatus() {
  return await chrome.runtime.sendMessage({ type: "GET_ACTIVE_STATUS" });
}

function render(s) {
  document.getElementById("videoId").textContent = s.videoId || "—";
  document.getElementById("visitCount").textContent = s.visitCount ?? "—";

  if (!s.url || (!s.videoId && s.url)) {
    setStatus("Not a YouTube video", "");
    document.getElementById("downloadBtn").disabled = true;
    return;
  }

  if (s.isDownloading) {
    setStatus("Downloading…", "dl");
    document.getElementById("downloadBtn").disabled = true;
    return;
  }

  if (s.isTrue === true) {
    setStatus("OK", "ok");
    document.getElementById("downloadBtn").disabled = true;
    return;
  }

  if (s.isTrue === false) {
    setStatus("NO (click Download)", "no");
    document.getElementById("downloadBtn").disabled = false;
    return;
  }

  setStatus("ERR / Unknown", "err");
  document.getElementById("downloadBtn").disabled = true;
}

async function refresh() {
  const res = await getStatus();
  if (!res?.ok) {
    setStatus("ERR", "err");
    return;
  }
  render(res);
}

document.getElementById("recheckBtn").addEventListener("click", async () => {
  setStatus("Rechecking…", "");
  await chrome.runtime.sendMessage({ type: "FORCE_RECHECK" });
  await refresh();
});

document.getElementById("downloadBtn").addEventListener("click", async () => {
  setStatus("Starting…", "dl");
  await chrome.runtime.sendMessage({ type: "TRIGGER_DOWNLOAD" });
  await refresh();
});

document.getElementById("openOptions").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

refresh();

let detectedPlaylists = [];
async function loadPlaylists() {
  const message = document.getElementById('playlistMessage');
  try {
    const tabs = await chrome.tabs.query({active: true, currentWindow: true});
    const tab = tabs[0];
    if (!tab?.id || !/youtube\.com/.test(tab.url || '')) { message.textContent = 'Open a YouTube channel or feed/playlists page first.'; return; }
    message.textContent = 'Scanning page and loading playlist links…';
    const result = await chrome.tabs.sendMessage(tab.id, {type: 'GET_PLAYLIST_LINKS'});
    detectedPlaylists = result?.playlists || [];
    if (!detectedPlaylists.length) { message.textContent = 'No playlist links found yet. Scroll the profile page to load more.'; return; }
    message.textContent = `${detectedPlaylists.length} playlist${detectedPlaylists.length === 1 ? '' : 's'} found`;
    const box = document.getElementById('playlistList'); box.hidden = false;
    box.innerHTML = detectedPlaylists.map((p, i) => `<label class="playlist-row"><input type="checkbox" data-playlist-index="${i}" checked><span>${escapeHtml(p.title)}<br><span class="muted">${escapeHtml(p.url)}</span></span></label>`).join('');
    document.getElementById('playlistActions').hidden = false;
  } catch { message.textContent = 'Could not scan this page; reload the extension on a YouTube channel page.'; }
}
function selectedPlaylists() { return [...document.querySelectorAll('[data-playlist-index]:checked')].map(row => detectedPlaylists[Number(row.dataset.playlistIndex)]).filter(Boolean); }
function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function exportSelected() {
  const text = selectedPlaylists().map(p => p.url).join('\n');
  const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([text], {type:'text/plain'})); link.download = 'youtube-playlists.txt'; link.click(); URL.revokeObjectURL(link.href);
}
async function postSelected(path, bodyFor) {
  const cfg = await chrome.storage.sync.get(['serverOrigin']);
  const origin = (cfg.serverOrigin || 'https://dihi.i.apiskpis.com').replace(/\/$/, '');
  const selected = selectedPlaylists(); let done = 0;
  for (const playlist of selected) { const response = await fetch(`${origin}${path(playlist.id)}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(bodyFor(playlist))}); if (response.ok) done++; }
  document.getElementById('playlistMessage').textContent = `${done} of ${selected.length} playlist${selected.length === 1 ? '' : 's'} processed`;
}
document.getElementById('exportPlaylists').addEventListener('click', exportSelected);
document.getElementById('savePlaylists').addEventListener('click', async event => { event.target.disabled = true; try { await postSelected(id => `/api/youtube/playlist/prepare/${encodeURIComponent(id)}`, () => ({})); } catch (e) { document.getElementById('playlistMessage').textContent = `Save failed: ${e.message}`; } event.target.disabled = false; });
document.getElementById('queuePlaylists').addEventListener('click', async event => { event.target.disabled = true; try { await postSelected(() => '/api/queue', p => ({target:p.url, start_now:false})); } catch (e) { document.getElementById('playlistMessage').textContent = `Queue failed: ${e.message}`; } event.target.disabled = false; });
loadPlaylists();
