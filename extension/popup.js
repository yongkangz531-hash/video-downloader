const HELPER = 'http://127.0.0.1:8791';
let TAB = null;
let ITEMS = [];

const $ = (s) => document.querySelector(s);

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtDur(s) {
  s = Math.round(s || 0);
  if (!s) return '';
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(x).padStart(2, '0');
}
function fmtSize(b) {
  if (!b) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n < 10 ? 1 : 0) + u[i];
}

function setMsg(t, cls) {
  const m = $('#msg');
  m.textContent = t || '';
  m.className = cls || '';
}

function render() {
  $('#count').textContent = ITEMS.length + ' 个';
  const box = $('#list');
  if (!ITEMS.length) {
    box.innerHTML = '<div class="empty">没有发现视频。<br>如果是要点播放才加载的站，<br>先在页面上播一下再点「扫描」。</div>';
    return;
  }
  box.innerHTML = ITEMS.map((it, i) => {
    let host = '';
    try { host = new URL(it.url).hostname; } catch (e) {}
    const tail = it.url.length > 86 ? it.url.slice(0, 86) + '…' : it.url;
    const tagCls = it.kind === 'HLS' ? 'tag hls' : (it.from === 'dom' ? 'tag dom' : 'tag');
    const meta = [it.height ? it.height + 'P' : '', it.duration ? fmtDur(it.duration) : '',
      it.size ? fmtSize(it.size) : ''].filter(Boolean).join(' · ');
    return '<div class="item">'
      + '<div class="row1">'
      + '<span class="' + tagCls + '">' + esc(it.kind || '视频') + '</span>'
      + '<span class="name">' + esc((it.title || host || '视频').slice(0, 60)) + (meta ? ' · ' + esc(meta) : '') + '</span>'
      + '</div>'
      + '<div class="url">' + esc(tail) + '</div>'
      + '<div class="acts">'
      + '<button class="pri" data-act="dl" data-i="' + i + '">下载</button>'
      + '<button data-act="copy" data-i="' + i + '">复制地址</button>'
      + '</div></div>';
  }).join('');
  box.querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      const it = ITEMS[parseInt(b.dataset.i, 10)];
      if (b.dataset.act === 'copy') {
        navigator.clipboard.writeText(it.url).then(() => setMsg('地址已复制', 'ok'));
      } else {
        dl(it, b);
      }
    };
  });
}

async function domScan() {
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: TAB.id },
      func: () => {
        const out = [];
        document.querySelectorAll('video').forEach(v => {
          const src = v.currentSrc || v.src || (v.querySelector('source') || {}).src || '';
          if (src && !src.startsWith('blob:')) {
            out.push({
              url: src, kind: '页面视频', from: 'dom',
              title: v.getAttribute('title') || document.title.slice(0, 80),
              height: v.videoHeight || 0,
              duration: isFinite(v.duration) ? v.duration : 0
            });
          }
        });
        return out;
      }
    });
    return (res && res.result) || [];
  } catch (e) {
    return [];
  }
}

async function refresh() {
  const net = await chrome.runtime.sendMessage({ type: 'get-items', tabId: TAB.id });
  const dom = await domScan();
  const merged = [];
  const seen = new Set();
  [...dom, ...((net && net.items) || [])].forEach(it => {
    if (!it || !it.url || seen.has(it.url)) return;
    seen.add(it.url);
    merged.push(it);
  });
  ITEMS = merged;
  render();
}

async function dl(item, btn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '提交中';
  setMsg('');
  try {
    let cookies = [];
    try {
      cookies = await chrome.cookies.getAll({ url: item.url });
    } catch (e) {}
    const r = await fetch(HELPER + '/api/dl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: item.url,
        referer: TAB.url,
        title: item.title || TAB.title || '视频',
        cookies: cookies || []
      })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '提交失败');
    setMsg('已交给本地下载器：' + (item.title || '视频') + '（' + (d.cookie_n || 0) + ' 条 cookie 已带上）', 'ok');
    btn.textContent = '已提交';
  } catch (e) {
    setMsg(/Failed to fetch/i.test(String(e))
      ? '连不上本地下载器（127.0.0.1:8791）。先双击桌面「视频下载器」启动它。'
      : (e.message || String(e)), 'err');
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  TAB = tab;
  try { $('#host').textContent = new URL(tab.url).hostname; } catch (e) { $('#host').textContent = '—'; }
  render();
  $('#scan').onclick = async () => {
    $('#scan').disabled = true;
    setMsg('正在扫描…');
    await refresh();
    setMsg(ITEMS.length ? '找到 ' + ITEMS.length + ' 个视频' : '没找到，试试在页面上播一下', ITEMS.length ? 'ok' : '');
    $('#scan').disabled = false;
  };
  $('#clear').onclick = async () => {
    await chrome.runtime.sendMessage({ type: 'clear', tabId: TAB.id });
    ITEMS = [];
    render();
    setMsg('已清空');
  };
  await refresh();
  if (ITEMS.length) setMsg('已发现 ' + ITEMS.length + ' 个视频', 'ok');
}

init();
