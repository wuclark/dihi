(() => {
  const id = new URL(location.href).searchParams.get('v') || location.pathname.split('/').pop();
  if (!id) return;
  chrome.storage.sync.get(['serverOrigin', 'timeoutMs', 'playArchivedFromServer', 'playbackMode'], async cfg => {
    if (cfg.playArchivedFromServer === false || cfg.playbackMode === 'redirect') return;
    const origin = (cfg.serverOrigin || 'http://localhost:5000').replace(/\/$/, '');
    try {
      const r = await fetch(`${origin}/api/media/resolve/${encodeURIComponent(id)}`);
      const data = await r.json();
      const media = data?.video?.player_url;
      if (!data?.result || !media) return;
      const use = cfg.playbackMode === 'ask' ? window.confirm('An archived DIHI copy is available. Play it locally?') : true;
      if (!use) return;
      const replace = () => {
        const host = document.querySelector('#movie_player, .html5-video-player');
        if (!host || host.dataset.dihiReplaced) return false;
        host.dataset.dihiReplaced = '1';
        host.innerHTML = '';
        const video = document.createElement('video');
        video.controls = true; video.autoplay = true; video.muted = true;
        video.style.cssText = 'width:100%;height:100%;background:#000;object-fit:contain';
        video.src = origin + media;
        host.appendChild(video);
        return true;
      };
      if (!replace()) new MutationObserver(() => replace()).observe(document.documentElement, {childList:true, subtree:true});
    } catch (_) {}
  });
})();
