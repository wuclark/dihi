(() => {
  'use strict';
  const playlistId = href => {
    try {
      const url = new URL(href, location.href);
      if (url.hostname.replace(/^www\./, '') !== 'youtube.com') return null;
      const id = url.searchParams.get('list');
      return id && /^[A-Za-z0-9_-]{2,128}$/.test(id) ? id : null;
    } catch { return null; }
  };
  const collect = () => {
    const found = new Map();
    const add = (id, title, href) => {
      if (!id || found.has(id)) return;
      found.set(id, {id, title: title || id, url: href || `https://www.youtube.com/playlist?list=${id}`});
    };
    document.querySelectorAll('a[href]').forEach(anchor => {
      const id = playlistId(anchor.href);
      const title = (anchor.getAttribute('aria-label') || anchor.textContent || '').replace(/\s+/g, ' ').trim();
      add(id, title, id ? `https://www.youtube.com/playlist?list=${id}` : null);
    });
    document.querySelectorAll('[data-list-id], [data-playlist-id]').forEach(node => {
      const id = node.getAttribute('data-list-id') || node.getAttribute('data-playlist-id');
      const title = (node.getAttribute('aria-label') || node.textContent || '').replace(/\s+/g, ' ').trim();
      add(id, title, null);
    });
    const markup = document.documentElement?.outerHTML || '';
    for (const match of markup.matchAll(/[?&]list=([A-Za-z0-9_-]{2,128})/g)) add(match[1], '', null);
    return [...found.values()];
  };
  const collectAll = async () => {
    const original = window.scrollY;
    let lastHeight = 0;
    let unchanged = 0;
    for (let pass = 0; pass < 40 && unchanged < 3; pass++) {
      window.scrollTo(0, document.documentElement.scrollHeight);
      await new Promise(resolve => setTimeout(resolve, 450));
      const height = document.documentElement.scrollHeight;
      unchanged = height === lastHeight ? unchanged + 1 : 0;
      lastHeight = height;
    }
    const playlists = collect();
    window.scrollTo(0, original);
    return playlists;
  };
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === 'GET_PLAYLIST_LINKS') {
      collectAll().then(playlists => sendResponse({ok: true, playlists, page: location.href}));
    }
    return true;
  });
})();
