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
