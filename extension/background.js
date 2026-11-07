// 后台：嗅探本标签页的媒体响应（看 Content-Type，不靠 URL 后缀）+ 记录页面里的 <video>
const MAX_ITEMS = 40;
const MEDIA_RE = /\.(mp4|m3u8|mpd|webm|flv|mov|mkv|m4v|mp3|m4a)(\?|#|$)/i;
const SKIP_RE = /(\.ts\b|\.m4s\b|\/seg[-_]|segment\d|\.jpe?g|\.png|\.gif|\.webp|\.svg|doubleclick|googleadservices|googlesyndication|beacon|analytics|sentry|\.css|\.woff)/i;
const MIN_SIZE = 150 * 1024; // 小于 150KB 的当封面/广告

function key(tabId) { return 't' + tabId; }

async function getItems(tabId) {
  const d = await chrome.storage.session.get(key(tabId));
  return d[key(tabId)] || [];
}

async function addItem(tabId, item) {
  const items = await getItems(tabId);
  if (items.some(x => x.url === item.url)) return;
  items.push(item);
  const trimmed = items.slice(-MAX_ITEMS);
  await chrome.storage.session.set({ [key(tabId)]: trimmed });
  chrome.action.setBadgeText({ tabId, text: String(trimmed.length) });
  chrome.action.setBadgeBackgroundColor({ tabId, color: '#7c5cff' });
}

async function clearTab(tabId) {
  await chrome.storage.session.remove(key(tabId));
  chrome.action.setBadgeText({ tabId, text: '' });
}

function kindOf(url, ct) {
  const low = url.toLowerCase();
  if (/mpegurl|m3u8/.test(ct) || /\.m3u8(\?|#|$)/.test(low)) return 'HLS';
  if (/dash\+xml/.test(ct) || /\.mpd(\?|#|$)/.test(low)) return 'DASH';
  if (ct.startsWith('audio/') || /\.(mp3|m4a)(\?|#|$)/.test(low)) return '音频';
  return '视频';
}

chrome.webRequest.onHeadersReceived.addListener(
  (d) => {
    if (d.tabId < 0 || !d.url) return;
    const url = d.url;
    if (/^(blob|data|chrome-extension):/.test(url)) return;
    if (SKIP_RE.test(url)) return;

    let ct = '', cl = 0;
    for (const h of (d.responseHeaders || [])) {
      const n = h.name.toLowerCase();
      if (n === 'content-type') ct = (h.value || '').toLowerCase();
      else if (n === 'content-length') cl = parseInt(h.value || '0', 10) || 0;
    }
    const isMediaCt = /^(video|audio)\//.test(ct) || /mpegurl|dash\+xml/.test(ct);
    if (!isMediaCt && !MEDIA_RE.test(url)) return;
    if (cl && cl < MIN_SIZE && !/mpegurl|dash\+xml/.test(ct) && !/\.(m3u8|mpd)(\?|#|$)/.test(url)) return;

    addItem(d.tabId, {
      url,
      kind: kindOf(url, ct),
      size: cl || 0,
      mime: ct,
      from: 'network',
      page: d.documentUrl || d.initiator || '',
      t: Date.now()
    });
  },
  { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'object'] },
  ['responseHeaders']
);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === 'dom-media' && sender.tab) {
    (msg.items || []).forEach(it => {
      if (!it.url || /^(blob|data):/.test(it.url)) return;
      addItem(sender.tab.id, {
        url: it.url, kind: it.kind || '页面视频', from: 'dom',
        page: sender.tab.url, title: it.title || '', height: it.height || 0,
        duration: it.duration || 0, size: 0, t: Date.now()
      });
    });
    sendResponse({ ok: true });
    return true;
  }
  if (msg && msg.type === 'get-items') {
    getItems(msg.tabId).then(items => sendResponse({ items }));
    return true;
  }
  if (msg && msg.type === 'clear') {
    clearTab(msg.tabId).then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

chrome.tabs.onRemoved.addListener((tabId) => { chrome.storage.session.remove(key(tabId)); });
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === 'loading') clearTab(tabId);
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'send-to-local',
    title: '用本地下载器打开本页',
    contexts: ['page', 'video', 'link']
  });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  const target = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!target) return;
  try {
    await chrome.tabs.create({ url: 'http://127.0.0.1:8791/?u=' + encodeURIComponent(target) });
  } catch (e) {
    console.warn(e);
  }
});
