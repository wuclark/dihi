const DEFAULTS = {
  serverOrigin: "https://dihi.i.apiskpis.com",
  timeoutMs: 6000,
  debounceMs: 600
};

// tabId -> { videoId, isTrue, lastCheckedAt }
const tabState = new Map();

// tabId -> { videoId, serverOrigin }
const downloadPollByTab = new Map();

function buildUrl(serverOrigin, path) {
  return `${serverOrigin.replace(/\/$/, "")}${path}`;
}

function extractYouTubeId(urlString) {
  let url;
  try {
    url = new URL(urlString);
  } catch {
    return null;
  }

  const host = url.hostname.replace(/^www\./, "");

  if (host === "youtube.com" || host === "m.youtube.com") {
    const v = url.searchParams.get("v");
    if (v) return v;

    const parts = url.pathname.split("/").filter(Boolean);
    if (parts[0] === "shorts" && parts[1]) return parts[1];

    return null;
  }

  if (host === "youtu.be") {
    const id = url.pathname.split("/").filter(Boolean)[0];
    return id || null;
  }

  return null;
}

function isYouTubeUrl(urlString) {
  try {
    const u = new URL(urlString);
    const h = u.hostname.replace(/^www\./, "");
    return h === "youtube.com" || h === "m.youtube.com" || h === "youtu.be";
  } catch {
    return false;
  }
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(t);
  }
}

async function setBadge(tabId, text, color) {
  await chrome.action.setBadgeBackgroundColor({ tabId, color });
  await chrome.action.setBadgeText({ tabId, text });
}

async function getConfig() {
  const cfg = await chrome.storage.sync.get(["serverOrigin", "timeoutMs", "debounceMs"]);
  return {
    serverOrigin: (cfg.serverOrigin || DEFAULTS.serverOrigin).replace(/\/$/, ""),
    timeoutMs: Number(cfg.timeoutMs || DEFAULTS.timeoutMs),
    debounceMs: Number(cfg.debounceMs || DEFAULTS.debounceMs)
  };
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.sync.get(["serverOrigin", "timeoutMs", "debounceMs"], (cfg) => {
    const updates = {};
    if (!cfg.serverOrigin) updates.serverOrigin = DEFAULTS.serverOrigin;
    if (!cfg.timeoutMs) updates.timeoutMs = DEFAULTS.timeoutMs;
    if (!cfg.debounceMs) updates.debounceMs = DEFAULTS.debounceMs;
    if (Object.keys(updates).length) chrome.storage.sync.set(updates);
  });
});

async function safeJson(res) {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

async function notifyDownloadFinished(videoId, ok = true) {
  try {
    await chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/icon128.png",
      title: ok ? "YouTube download finished" : "YouTube download update",
      message: ok
        ? `Server finished processing video ID: ${videoId}`
        : `Update for video ID: ${videoId}`
    });
  } catch {
    // ignore notification failures
  }
}

async function checkTab(tabId, tabUrl) {
  if (!tabUrl || !isYouTubeUrl(tabUrl)) {
    await setBadge(tabId, "", "#808080");
    tabState.delete(tabId);
    return { status: "not_youtube" };
  }

  const videoId = extractYouTubeId(tabUrl);
  if (!videoId) {
    await setBadge(tabId, "—", "#808080");
    tabState.delete(tabId);
    return { status: "no_video_id" };
  }

  const { serverOrigin, timeoutMs, debounceMs } = await getConfig();

  const now = Date.now();
  const prev = tabState.get(tabId);
  if (prev && prev.videoId === videoId && (now - prev.lastCheckedAt) < debounceMs) {
    return { status: "debounced", videoId, isTrue: prev.isTrue };
  }

  const checkUrl = buildUrl(serverOrigin, `/api/youtube/${encodeURIComponent(videoId)}`);

  tabState.set(tabId, { videoId, isTrue: null, lastCheckedAt: now });

  await setBadge(tabId, "...", "#1a73e8");

  try {
    const res = await fetchWithTimeout(checkUrl, { method: "GET" }, timeoutMs);
    const data = await safeJson(res);

    const isTrue = data?.result === true;

    tabState.set(tabId, { videoId, isTrue, lastCheckedAt: now });

    if (isTrue) {
      await setBadge(tabId, "OK", "#00A000");
    } else {
      await setBadge(tabId, "NO", "#D00000");
    }

    return { status: "checked", videoId, isTrue };
  } catch {
    tabState.set(tabId, { videoId, isTrue: null, lastCheckedAt: now });
    await setBadge(tabId, "ERR", "#D00000");
    return { status: "error", videoId };
  }
}

async function startDownloadFlow(tabId, tabUrl) {
  await checkTab(tabId, tabUrl);

  const state = tabState.get(tabId);
  if (!state?.videoId) return { ok: false, reason: "no_video_id" };

  if (state.isTrue !== false) return { ok: false, reason: "not_red" };

  const { serverOrigin, timeoutMs } = await getConfig();
  const postUrl = buildUrl(serverOrigin, `/api/youtube/get/${encodeURIComponent(state.videoId)}`);

  try {
    const res = await fetchWithTimeout(
      postUrl,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ videoId: state.videoId })
      },
      timeoutMs
    );

    if (!res.ok) {
      await setBadge(tabId, "ERR", "#D00000");
      return { ok: false, reason: "post_failed", status: res.status };
    }

    await setBadge(tabId, "DL", "#FFD000");

    downloadPollByTab.set(tabId, { videoId: state.videoId, serverOrigin });
    chrome.alarms.create(`poll_${tabId}`, { periodInMinutes: 0.05 });

    return { ok: true, videoId: state.videoId };
  } catch {
    await setBadge(tabId, "ERR", "#D00000");
    return { ok: false, reason: "exception" };
  }
}

chrome.webNavigation.onHistoryStateUpdated.addListener(
  async (details) => {
    if (details.frameId !== 0) return;
    await checkTab(details.tabId, details.url);
  },
  {
    url: [{ hostSuffix: "youtube.com" }, { hostEquals: "youtu.be" }]
  }
);

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.url) {
    await checkTab(tabId, changeInfo.url);
  } else if (changeInfo.status === "complete" && tab?.url) {
    await checkTab(tabId, tab.url);
  }
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await chrome.tabs.get(tabId);
  if (tab?.url) await checkTab(tabId, tab.url);
});

chrome.action.onClicked.addListener(async (tab) => {
  const tabId = tab?.id;
  const tabUrl = tab?.url || "";
  if (!tabId) return;
  await startDownloadFlow(tabId, tabUrl);
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (!alarm?.name?.startsWith("poll_")) return;

  const tabId = Number(alarm.name.replace("poll_", ""));
  const info = downloadPollByTab.get(tabId);

  if (!info) {
    chrome.alarms.clear(alarm.name);
    return;
  }

  const { videoId, serverOrigin } = info;
  const statusUrl = buildUrl(serverOrigin, `/api/youtube/status/${encodeURIComponent(videoId)}`);

  try {
    const { timeoutMs } = await getConfig();
    const res = await fetchWithTimeout(statusUrl, { method: "GET" }, timeoutMs);
    const data = await safeJson(res);

    if (data?.downloading === true) {
      await setBadge(tabId, "DL", "#FFD000");
      return;
    }

    // done
    downloadPollByTab.delete(tabId);
    chrome.alarms.clear(alarm.name);

    // Notify user download finished
    await notifyDownloadFinished(videoId, true);

    const tab = await chrome.tabs.get(tabId);
    if (tab?.url) {
      await checkTab(tabId, tab.url);
    } else {
      await setBadge(tabId, "OK", "#00A000");
    }
  } catch {
    await setBadge(tabId, "ERR", "#D00000");
    // Optional: notify on error
    await notifyDownloadFinished(info.videoId, false);
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg?.type === "GET_ACTIVE_STATUS") {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) return sendResponse({ ok: false });

      if (tab.url) await checkTab(tab.id, tab.url);

      const state = tabState.get(tab.id) || null;
      const polling = downloadPollByTab.has(tab.id);

      return sendResponse({
        ok: true,
        tabId: tab.id,
        url: tab.url || "",
        videoId: state?.videoId || null,
        isTrue: state?.isTrue ?? null,
        isDownloading: polling
      });
    }

    if (msg?.type === "FORCE_RECHECK") {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id || !tab.url) return sendResponse({ ok: false });
      const res = await checkTab(tab.id, tab.url);
      return sendResponse({ ok: true, result: res });
    }

    if (msg?.type === "TRIGGER_DOWNLOAD") {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id || !tab.url) return sendResponse({ ok: false });
      const res = await startDownloadFlow(tab.id, tab.url);
      return sendResponse({ ok: true, result: res });
    }

    return sendResponse({ ok: false, error: "unknown_message" });
  })();

  return true;
});
