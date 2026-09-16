const DEFAULTS = {
  serverOrigin: "https://dihi.i.apiskpis.com",
  timeoutMs: 6000,
  debounceMs: 600,
  autoDownloadEnabled: false,
  autoDownloadVisitThreshold: 3,
  playArchivedFromServer: true
  ,playbackMode: "redirect"
};

function showSaved() {
  const el = document.getElementById("savedMsg");
  el.style.display = "inline";
  setTimeout(() => (el.style.display = "none"), 1200);
}

async function load() {
  const cfg = await chrome.storage.sync.get([
    "serverOrigin",
    "timeoutMs",
    "debounceMs",
    "autoDownloadEnabled",
    "autoDownloadVisitThreshold",
    "playArchivedFromServer", "playbackMode"
  ]);
  document.getElementById("serverOrigin").value = cfg.serverOrigin || DEFAULTS.serverOrigin;
  document.getElementById("timeoutMs").value = Number(cfg.timeoutMs || DEFAULTS.timeoutMs);
  document.getElementById("debounceMs").value = Number(cfg.debounceMs || DEFAULTS.debounceMs);
  document.getElementById("autoDownloadEnabled").checked = Boolean(
    cfg.autoDownloadEnabled ?? DEFAULTS.autoDownloadEnabled
  );
  document.getElementById("autoDownloadVisitThreshold").value = Number(
    cfg.autoDownloadVisitThreshold || DEFAULTS.autoDownloadVisitThreshold
  );
  document.getElementById("playArchivedFromServer").checked = Boolean(
    cfg.playArchivedFromServer ?? DEFAULTS.playArchivedFromServer
  );
  document.getElementById("playbackMode").value = ["redirect", "inpage", "ask"].includes(cfg.playbackMode) ? cfg.playbackMode : DEFAULTS.playbackMode;
  updateServerLink(cfg.serverOrigin || DEFAULTS.serverOrigin);
}

function updateServerLink(origin) {
  document.getElementById("serverLink").href = origin.replace(/\/$/, "");
}

async function save() {
  const serverOrigin = (document.getElementById("serverOrigin").value || DEFAULTS.serverOrigin).trim().replace(/\/$/, "");
  const timeoutMs = Number(document.getElementById("timeoutMs").value || DEFAULTS.timeoutMs);
  const debounceMs = Number(document.getElementById("debounceMs").value || DEFAULTS.debounceMs);
  const autoDownloadEnabled = document.getElementById("autoDownloadEnabled").checked;
  const autoDownloadVisitThreshold = Math.max(
    1,
    Number(document.getElementById("autoDownloadVisitThreshold").value || DEFAULTS.autoDownloadVisitThreshold)
  );
  const playArchivedFromServer = document.getElementById("playArchivedFromServer").checked;
  const playbackMode = document.getElementById("playbackMode").value;

  await chrome.storage.sync.set({
    serverOrigin,
    timeoutMs,
    debounceMs,
    autoDownloadEnabled,
    autoDownloadVisitThreshold,
    playArchivedFromServer, playbackMode
  });
  updateServerLink(serverOrigin);
  showSaved();
}

async function reset() {
  await chrome.storage.sync.set({ ...DEFAULTS });
  await load();
  showSaved();
}

document.getElementById("saveBtn").addEventListener("click", save);
document.getElementById("resetBtn").addEventListener("click", reset);

load();
