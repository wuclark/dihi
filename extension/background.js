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

// Check video status and update badge
async function checkVideoStatus(tabId, videoId) {
  if (!videoId) {
    await updateBadge(tabId, 'gray');
    return;
  }

  const apiUrl = await getApiUrl();

  try {
    // Check download status first
    const statusResponse = await fetch(`${apiUrl}/api/youtube/status/${videoId}`);

    if (statusResponse.ok) {
      const statusData = await statusResponse.json();

      if (statusData.downloading) {
        await updateBadge(tabId, 'yellow');
        return;
      }
    }

    // Check archive status
    const archiveResponse = await fetch(`${apiUrl}/api/youtube/${videoId}`);

    if (archiveResponse.ok) {
      const archiveData = await archiveResponse.json();
      await updateBadge(tabId, archiveData.result ? 'green' : 'red');
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
    checkVideoStatus(sender.tab.id, message.videoId);
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
