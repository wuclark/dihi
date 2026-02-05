// Background service worker
// Handles badge updates and API communication

const DEFAULT_API_URL = 'https://dihi.i.apiskpis.com';

// Badge colors
const BADGE_COLORS = {
  red: '#ef4444',
  green: '#22c55e',
  yellow: '#eab308',
  gray: '#9ca3af'
};

// Cache for video statuses (videoId -> {status, timestamp})
const statusCache = new Map();
const CACHE_TTL = 30000; // 30 seconds cache TTL

// Track tabs with active downloads for polling
const activeDownloadTabs = new Map(); // tabId -> {videoId, intervalId}

// Get API URL from storage
async function getApiUrl() {
  const stored = await chrome.storage.local.get(['apiUrl']);
  return stored.apiUrl || DEFAULT_API_URL;
}

// Update badge for a tab
async function updateBadge(tabId, status) {
  const colors = {
    red: BADGE_COLORS.red,
    green: BADGE_COLORS.green,
    yellow: BADGE_COLORS.yellow,
    gray: BADGE_COLORS.gray
  };

  const texts = {
    red: '!',
    green: '\u2713',
    yellow: '\u2193',
    gray: '?'
  };

  try {
    await chrome.action.setBadgeBackgroundColor({
      color: colors[status] || colors.gray,
      tabId: tabId
    });

    await chrome.action.setBadgeText({
      text: texts[status] || '',
      tabId: tabId
    });
  } catch {
    // Tab might be closed
  }
}

// Get cached status if valid
function getCachedStatus(videoId) {
  const cached = statusCache.get(videoId);
  if (cached && Date.now() - cached.timestamp < CACHE_TTL) {
    return cached.status;
  }
  return null;
}

// Set cached status
function setCachedStatus(videoId, status) {
  statusCache.set(videoId, {
    status: status,
    timestamp: Date.now()
  });
}

// Clear cache for a video (when download starts)
function clearCachedStatus(videoId) {
  statusCache.delete(videoId);
}

// Start polling for download completion
function startDownloadPolling(tabId, videoId) {
  // Clear any existing polling for this tab
  stopDownloadPolling(tabId);

  // Poll every 3 seconds
  const intervalId = setInterval(async () => {
    try {
      const apiUrl = await getApiUrl();
      const response = await fetch(`${apiUrl}/api/youtube/status/${videoId}`);

      if (response.ok) {
        const data = await response.json();

        if (!data.downloading) {
          // Download finished - check archive status
          stopDownloadPolling(tabId);
          clearCachedStatus(videoId);
          await checkVideoStatus(tabId, videoId, true); // Force refresh
        }
      }
    } catch {
      // Ignore polling errors
    }
  }, 3000);

  activeDownloadTabs.set(tabId, { videoId, intervalId });
}

// Stop polling for a tab
function stopDownloadPolling(tabId) {
  const existing = activeDownloadTabs.get(tabId);
  if (existing) {
    clearInterval(existing.intervalId);
    activeDownloadTabs.delete(tabId);
  }
}

// Check video status and update badge
async function checkVideoStatus(tabId, videoId, forceRefresh = false) {
  if (!videoId) {
    await updateBadge(tabId, 'gray');
    return;
  }

  // Check cache first (unless forcing refresh)
  if (!forceRefresh) {
    const cachedStatus = getCachedStatus(videoId);
    if (cachedStatus) {
      await updateBadge(tabId, cachedStatus);

      // If cached as downloading, ensure polling is active
      if (cachedStatus === 'yellow') {
        if (!activeDownloadTabs.has(tabId)) {
          startDownloadPolling(tabId, videoId);
        }
      }
      return;
    }
  }

  const apiUrl = await getApiUrl();

  try {
    // Check download status first
    const statusResponse = await fetch(`${apiUrl}/api/youtube/status/${videoId}`);

    if (statusResponse.ok) {
      const statusData = await statusResponse.json();

      if (statusData.downloading) {
        await updateBadge(tabId, 'yellow');
        setCachedStatus(videoId, 'yellow');
        startDownloadPolling(tabId, videoId);
        return;
      }
    }

    // Check archive status
    const archiveResponse = await fetch(`${apiUrl}/api/youtube/${videoId}`);

    if (archiveResponse.ok) {
      const archiveData = await archiveResponse.json();
      const status = archiveData.result ? 'green' : 'red';
      await updateBadge(tabId, status);
      setCachedStatus(videoId, status);
      stopDownloadPolling(tabId); // Stop any polling if not downloading
    } else {
      await updateBadge(tabId, 'gray');
    }

  } catch {
    await updateBadge(tabId, 'gray');
  }
}

// Extract video ID from URL
function extractVideoId(url) {
  try {
    const urlObj = new URL(url);

    if (urlObj.hostname.includes('youtube.com')) {
      if (urlObj.pathname === '/watch') {
        return urlObj.searchParams.get('v');
      }
      if (urlObj.pathname.startsWith('/shorts/')) {
        return urlObj.pathname.split('/shorts/')[1]?.split('/')[0];
      }
      if (urlObj.pathname.startsWith('/embed/')) {
        return urlObj.pathname.split('/embed/')[1]?.split('/')[0];
      }
    }

    if (urlObj.hostname === 'youtu.be') {
      return urlObj.pathname.slice(1).split('/')[0];
    }

    return null;
  } catch {
    return null;
  }
}

// Listen for messages from content script
chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.type === 'VIDEO_CHANGED' && sender.tab) {
    // Clear cache for instant feedback on navigation
    if (message.videoId) {
      clearCachedStatus(message.videoId);
    }
    checkVideoStatus(sender.tab.id, message.videoId, true);
  }

  // Handle download started notification from popup
  if (message.type === 'DOWNLOAD_STARTED' && message.tabId && message.videoId) {
    clearCachedStatus(message.videoId);
    updateBadge(message.tabId, 'yellow');
    setCachedStatus(message.videoId, 'yellow');
    startDownloadPolling(message.tabId, message.videoId);
  }
});

// Listen for tab updates
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url) {
    const videoId = extractVideoId(tab.url);
    if (videoId) {
      checkVideoStatus(tabId, videoId);
    } else {
      // Clear badge for non-YouTube video pages
      stopDownloadPolling(tabId);
      chrome.action.setBadgeText({ text: '', tabId: tabId }).catch(() => {});
    }
  }
});

// Listen for tab activation
chrome.tabs.onActivated.addListener(async (activeInfo) => {
  try {
    const tab = await chrome.tabs.get(activeInfo.tabId);
    if (tab.url) {
      const videoId = extractVideoId(tab.url);
      if (videoId) {
        checkVideoStatus(activeInfo.tabId, videoId);
      } else {
        chrome.action.setBadgeText({ text: '', tabId: activeInfo.tabId }).catch(() => {});
      }
    }
  } catch {
    // Tab might not exist
  }
});

// Clean up when tab is closed
chrome.tabs.onRemoved.addListener((tabId) => {
  stopDownloadPolling(tabId);
});

// Periodic cache cleanup (every 5 minutes)
setInterval(() => {
  const now = Date.now();
  for (const [videoId, cached] of statusCache.entries()) {
    if (now - cached.timestamp > CACHE_TTL * 2) {
      statusCache.delete(videoId);
    }
  }
}, 300000);
