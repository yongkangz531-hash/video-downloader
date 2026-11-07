// 页面脚本：扫描 <video> / <source>，并尝试让播放器动起来
(function () {
  let lastSent = '';

  function collect() {
    const out = [];
    document.querySelectorAll('video').forEach((v) => {
      let src = v.currentSrc || v.src || '';
      if (!src) {
        const s = v.querySelector('source');
        if (s) src = s.src || '';
      }
      if (!src || src.startsWith('blob:') || src.startsWith('data:')) return;
      out.push({
        url: src,
        kind: '页面视频',
        title: (v.getAttribute('title') || document.title || '').slice(0, 120),
        height: v.videoHeight || 0,
        duration: isFinite(v.duration) ? v.duration : 0
      });
    });
    document.querySelectorAll('a[href]').forEach((a) => {
      const h = a.getAttribute('href') || '';
      if (/\.(mp4|m3u8|webm|flv|mov)(\?|#|$)/i.test(h)) {
        try { out.push({ url: new URL(h, location.href).href, kind: '页面链接' }); } catch (e) {}
      }
    });
    return out;
  }

  function report() {
    const items = collect();
    const sig = items.map(i => i.url).join('|');
    if (!items.length || sig === lastSent) return;
    lastSent = sig;
    try {
      chrome.runtime.sendMessage({ type: 'dom-media', items });
    } catch (e) {
      // 扩展被重载后旧脚本会失效，忽略
    }
  }

  // 站点常要 play() 才真正拉流
  function kickPlay() {
    document.querySelectorAll('video').forEach((v) => {
      try {
        if (v.paused) { v.muted = true; v.play().catch(() => {}); }
      } catch (e) {}
    });
  }

  report();
  setTimeout(() => { report(); kickPlay(); }, 1200);
  setTimeout(report, 3500);

  const mo = new MutationObserver(() => {
    clearTimeout(window.__vdT);
    window.__vdT = setTimeout(report, 900);
  });
  try {
    mo.observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}

  document.addEventListener('click', () => setTimeout(report, 800), true);
  window.addEventListener('load', () => setTimeout(report, 1000));
})();
