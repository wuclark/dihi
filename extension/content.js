// Content script for YouTube pages
// Detects video ID changes and notifies the background service worker

let lastVideoId = null;
let lastUrl = window.location.href;

// Extract video ID from current URL
function extractVideoId() {
  const url = new URL(window.location.href);

  // Standard watch URL
  if (url.pathname === '/watch') {
    return url.searchParams.get('v');
  }

  // Shorts URL
  if (url.pathname.startsWith('/shorts/')) {
    return url.pathname.split('/shorts/')[1]?.split('/')[0];
  }

  // Embed URL
  if (url.pathname.startsWith('/embed/')) {
    return url.pathname.split('/embed/')[1]?.split('/')[0];
  }

  return null;
}

// Debounce function to prevent excessive API calls
function debounce(func, wait) {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
}

// Check for video ID changes and notify background
function checkVideoChange() {
  const currentUrl = window.location.href;
  const videoId = extractVideoId();

  // Check if URL or video ID changed
  if (currentUrl !== lastUrl || (videoId && videoId !== lastVideoId)) {
    lastUrl = currentUrl;

    if (videoId !== lastVideoId) {
      lastVideoId = videoId;

      // Notify background script of video change
      chrome.runtime.sendMessage({
        type: 'VIDEO_CHANGED',
        videoId: videoId,
        url: currentUrl
      }).catch(() => {
        // Ignore errors if background script isn't ready
      });
    }
  }
}

// Debounced version for observers
const debouncedCheck = debounce(checkVideoChange, 100);

// Initial check
checkVideoChange();

// YouTube is a SPA, so we need multiple detection methods

// 1. MutationObserver on the document body for DOM changes
const bodyObserver = new MutationObserver(() => {
  debouncedCheck();
});

bodyObserver.observe(document.body, {
  childList: true,
  subtree: true
});

// 2. Observe the title element for changes
const titleElement = document.querySelector('title');
if (titleElement) {
  const titleObserver = new MutationObserver(() => {
    debouncedCheck();
  });

  titleObserver.observe(titleElement, {
    subtree: true,
    characterData: true,
    childList: true
  });
}

// 3. Listen for popstate events (browser back/forward)
window.addEventListener('popstate', checkVideoChange);

// 4. Listen for YouTube's custom navigation events
window.addEventListener('yt-navigate-finish', checkVideoChange);
window.addEventListener('yt-navigate-start', checkVideoChange);
window.addEventListener('yt-page-data-updated', checkVideoChange);

// 5. Intercept history API to catch programmatic navigation
const originalPushState = history.pushState;
const originalReplaceState = history.replaceState;

history.pushState = function(...args) {
  originalPushState.apply(this, args);
  setTimeout(checkVideoChange, 0);
};

history.replaceState = function(...args) {
  originalReplaceState.apply(this, args);
  setTimeout(checkVideoChange, 0);
};

// 6. Periodic check as a fallback (every 2 seconds)
setInterval(checkVideoChange, 2000);

// 7. Check on visibility change (when tab becomes visible)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    checkVideoChange();
  }
});
