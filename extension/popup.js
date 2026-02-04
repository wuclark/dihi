// Default API URL
const DEFAULT_API_URL = 'http://localhost:5000';

// DOM elements
const youtubeContent = document.getElementById('youtube-content');
const notYoutube = document.getElementById('not-youtube');
const statusCircle = document.getElementById('status-circle');
const statusText = document.getElementById('status-text');
const videoIdEl = document.getElementById('video-id');
const downloadBtn = document.getElementById('download-btn');
const messageEl = document.getElementById('message');
const apiUrlInput = document.getElementById('api-url');

let currentVideoId = null;
let statusPollInterval = null;

// Initialize
document.addEventListener('DOMContentLoaded', async () => {
  // Load saved API URL
  const stored = await chrome.storage.local.get(['apiUrl']);
  const apiUrl = stored.apiUrl || DEFAULT_API_URL;
  apiUrlInput.value = apiUrl;

  // Save API URL on change
  apiUrlInput.addEventListener('change', () => {
    chrome.storage.local.set({ apiUrl: apiUrlInput.value.trim() || DEFAULT_API_URL });
  });

  // Get current tab
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  if (tab && tab.url) {
    const videoId = extractVideoId(tab.url);

    if (videoId) {
      currentVideoId = videoId;
      youtubeContent.style.display = 'block';
      notYoutube.style.display = 'none';
      videoIdEl.textContent = videoId;

      // Check archive status
      await checkStatus();

      // Set up download button
      downloadBtn.addEventListener('click', initiateDownload);
    } else {
      youtubeContent.style.display = 'none';
      notYoutube.style.display = 'block';
    }
  } else {
    youtubeContent.style.display = 'none';
    notYoutube.style.display = 'block';
  }
});

// Extract YouTube video ID from URL
function extractVideoId(url) {
  try {
    const urlObj = new URL(url);

    // Standard watch URL
    if (urlObj.hostname.includes('youtube.com')) {
      if (urlObj.pathname === '/watch') {
        return urlObj.searchParams.get('v');
      }
      // Shorts URL
      if (urlObj.pathname.startsWith('/shorts/')) {
        return urlObj.pathname.split('/shorts/')[1]?.split('/')[0];
      }
      // Embed URL
      if (urlObj.pathname.startsWith('/embed/')) {
        return urlObj.pathname.split('/embed/')[1]?.split('/')[0];
      }
    }

    // youtu.be short URL
    if (urlObj.hostname === 'youtu.be') {
      return urlObj.pathname.slice(1).split('/')[0];
    }

    return null;
  } catch {
    return null;
  }
}

// Get API URL
function getApiUrl() {
  return apiUrlInput.value.trim() || DEFAULT_API_URL;
}

// Set status display
function setStatus(status, text) {
  statusCircle.className = 'status-circle ' + status;
  statusText.textContent = text;

  // Update button state
  if (status === 'green') {
    downloadBtn.disabled = true;
    downloadBtn.textContent = 'Already in Archive';
  } else if (status === 'yellow') {
    downloadBtn.disabled = true;
    downloadBtn.textContent = 'Downloading...';
  } else if (status === 'red') {
    downloadBtn.disabled = false;
    downloadBtn.textContent = 'Download Video';
  } else {
    downloadBtn.disabled = true;
    downloadBtn.textContent = 'Download Video';
  }
}

// Show message
function showMessage(text, type) {
  messageEl.textContent = text;
  messageEl.className = 'message ' + type;
  messageEl.style.display = 'block';

  // Auto-hide after 5 seconds
  setTimeout(() => {
    messageEl.style.display = 'none';
  }, 5000);
}

// Check archive and download status
async function checkStatus() {
  if (!currentVideoId) return;

  const apiUrl = getApiUrl();

  try {
    // First check if download is in progress
    const statusResponse = await fetch(`${apiUrl}/api/youtube/status/${currentVideoId}`);

    if (statusResponse.ok) {
      const statusData = await statusResponse.json();

      if (statusData.downloading) {
        setStatus('yellow', 'Download in progress');
        startStatusPolling();
        return;
      }
    }

    // Check archive status
    const archiveResponse = await fetch(`${apiUrl}/api/youtube/${currentVideoId}`);

    if (!archiveResponse.ok) {
      throw new Error(`HTTP ${archiveResponse.status}`);
    }

    const archiveData = await archiveResponse.json();

    if (archiveData.result) {
      setStatus('green', 'In archive');
    } else {
      setStatus('red', 'Not in archive');
    }

  } catch (error) {
    setStatus('gray', 'Cannot connect to API');
    showMessage(`Error: ${error.message}`, 'error');
  }
}

// Initiate download
async function initiateDownload() {
  if (!currentVideoId) return;

  const apiUrl = getApiUrl();

  setStatus('yellow', 'Starting download...');

  try {
    const response = await fetch(`${apiUrl}/api/youtube/get/${currentVideoId}`, {
      method: 'POST'
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const data = await response.json();

    if (data.ok) {
      if (data.already_running) {
        showMessage('Download already in progress', 'success');
      } else {
        showMessage('Download started', 'success');
      }
      setStatus('yellow', 'Download in progress');
      startStatusPolling();
    } else {
      throw new Error(data.error || 'Unknown error');
    }

  } catch (error) {
    setStatus('red', 'Not in archive');
    showMessage(`Error: ${error.message}`, 'error');
  }
}

// Poll for status updates
function startStatusPolling() {
  // Clear existing interval
  if (statusPollInterval) {
    clearInterval(statusPollInterval);
  }

  // Poll every 3 seconds
  statusPollInterval = setInterval(async () => {
    if (!currentVideoId) {
      clearInterval(statusPollInterval);
      return;
    }

    const apiUrl = getApiUrl();

    try {
      const response = await fetch(`${apiUrl}/api/youtube/status/${currentVideoId}`);

      if (response.ok) {
        const data = await response.json();

        if (!data.downloading) {
          // Download finished, check archive status
          clearInterval(statusPollInterval);
          await checkStatus();
        }
      }
    } catch {
      // Ignore polling errors
    }
  }, 3000);
}

// Cleanup on popup close
window.addEventListener('unload', () => {
  if (statusPollInterval) {
    clearInterval(statusPollInterval);
  }
});
