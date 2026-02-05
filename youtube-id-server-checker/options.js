const DEFAULTS = {
  serverOrigin: "https://dihi.i.apiskpis.com",
  timeoutMs: 6000,
  debounceMs: 600
};

function showSaved() {
  const el = document.getElementById("savedMsg");
  el.style.display = "inline";
  setTimeout(() => (el.style.display = "none"), 1200);
}

async function load() {
  const cfg = await chrome.storage.sync.get(["serverOrigin", "timeoutMs", "debounceMs"]);
  document.getElementById("serverOrigin").value = cfg.serverOrigin || DEFAULTS.serverOrigin;
  document.getElementById("timeoutMs").value = Number(cfg.timeoutMs || DEFAULTS.timeoutMs);
  document.getElementById("debounceMs").value = Number(cfg.debounceMs || DEFAULTS.debounceMs);
}

async function save() {
  const serverOrigin = (document.getElementById("serverOrigin").value || DEFAULTS.serverOrigin).trim().replace(/\/$/, "");
  const timeoutMs = Number(document.getElementById("timeoutMs").value || DEFAULTS.timeoutMs);
  const debounceMs = Number(document.getElementById("debounceMs").value || DEFAULTS.debounceMs);

  await chrome.storage.sync.set({ serverOrigin, timeoutMs, debounceMs });
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
