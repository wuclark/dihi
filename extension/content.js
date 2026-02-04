// Content script for YouTube pages
// Detects video ID changes and notifies the background service worker

let lastVideoId = null;

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

// Check for video ID changes
function checkVideoChange() {
  const videoId = extractVideoId();

  if (videoId && videoId !== lastVideoId) {
    lastVideoId = videoId;

    // Notify background script of video change
    chrome.runtime.sendMessage({
      type: 'VIDEO_CHANGED',
      videoId: videoId
    }).catch(() => {
      // Ignore errors if background script isn't ready
    });
  }
}

// Initial check
checkVideoChange();

// YouTube is a SPA, so we need to watch for URL changes
// Use MutationObserver to detect navigation
const observer = new MutationObserver(() => {
  checkVideoChange();
});

// Observe the title element for changes (indicates page navigation)
observer.observe(document.querySelector('title') || document.head, {
  subtree: true,
  characterData: true,
  childList: true
});

// Also listen for popstate events
window.addEventListener('popstate', checkVideoChange);

// Listen for YouTube's custom navigation events
window.addEventListener('yt-navigate-finish', checkVideoChange);
