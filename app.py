# -*- coding: utf-8 -*-
"""本地网页版视频下载器 —— 粘贴链接，解析，直接下载。

零第三方依赖（只用标准库），底层调用本机 yt-dlp.exe + ffmpeg。
"""
import json
import os
import re
import select
import shutil
import socket
import struct
import base64
import subprocess
import sys
import threading
import time
import uuid
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("VD_PORT") or 8791)
BASE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(BASE, "tools")
DL_DIR = os.path.join(BASE, "downloads")
os.makedirs(DL_DIR, exist_ok=True)
os.makedirs(TOOLS, exist_ok=True)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PROXY = os.environ.get("VD_PROXY") or "http://127.0.0.1:7897"
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ---------------------------------------------------------------- 站点适配
CCTV_RE = re.compile(r"cctv\.com/[^\"'\s]*[?&](?:guid|pid)=([0-9a-fA-F]{16,})")


def resolve_site(url):
    """央视类页面 → 真实 m3u8 地址 + 元信息。其它站点原样返回。"""
    m = CCTV_RE.search(url)
    if not m:
        return url, None
    pid = m.group(1)
    api = f"https://vdn.apps.cntv.cn/api/getHttpVideoInfo.do?pid={pid}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": UA,
                                                  "Referer": "https://app.cctv.com/"})
        with urllib.request.urlopen(req, timeout=25) as f:
            d = json.loads(f.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return url, None
    stream = d.get("hls_url") or (d.get("manifest") or {}).get("hls_h5e_url") \
        or (d.get("manifest") or {}).get("audio_mp3")
    if not stream:
        return url, None
    meta = {
        "title": d.get("title") or "",
        "image": d.get("image") or "",
        "duration": float(d.get("video", {}).get("totalLength") or 0),
        "channel": d.get("play_channel") or "",
        "pgmtime": d.get("f_pgmtime") or "",
    }
    return stream, meta


# ---------------------------------------------------------------- yt-dlp 定位
def find_ytdlp():
    for c in [os.path.join(TOOLS, "yt-dlp.exe"), os.path.join(TOOLS, "yt-dlp"),
              shutil.which("yt-dlp"),
              os.path.expanduser(r"~/.local/bin/yt-dlp.exe"),
              os.path.expanduser(r"~/.local/bin/yt-dlp"),
              os.path.expanduser(r"~/AppData/Local/Microsoft/WinGet/Links/yt-dlp.exe")]:
        if c and os.path.exists(c):
            return c
    return None


def fetch(url, dest, timeout=120):
    """直连失败就走本机代理再试一次。"""
    last = None
    for proxy in (None, PROXY):
        try:
            handlers = []
            if proxy:
                handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with opener.open(req, timeout=timeout) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 256 * 1024)
            if os.path.getsize(dest) > 1024:
                return dest
        except Exception as e:  # noqa: BLE001
            last = e
            continue
    raise RuntimeError(f"下载失败：{last}")


YTDLP = find_ytdlp()
COOKIE_FILE = os.path.join(BASE, "cookies.txt")
FFMPEG_DIR = None


def find_ffmpeg_dir():
    for p in (os.path.join(TOOLS, "ffmpeg.exe"),
              os.path.join(TOOLS, "ffmpeg", "bin", "ffmpeg.exe")):
        if os.path.exists(p):
            return os.path.dirname(p)
    w = shutil.which("ffmpeg")
    return os.path.dirname(w) if w else None


FFMPEG_DIR = find_ffmpeg_dir()


def ensure_engine():
    """没有 yt-dlp 就现下；换台电脑双击就能跑。"""
    global YTDLP
    if YTDLP and os.path.exists(YTDLP):
        return True
    if not _auto_fetch_ok:
        return False
    dest = os.path.join(TOOLS, "yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    try:
        print("[*] 首次运行，正在下载 yt-dlp（约 20MB）…")
        fetch(YTDLP_URL, dest, timeout=300)
        if os.name != "nt":
            os.chmod(dest, 0o755)
        YTDLP = dest
        print("[*] yt-dlp 就绪:", dest)
        return True
    except Exception as e:  # noqa: BLE001
        print("[!] 自动下载 yt-dlp 失败:", e)
        return False


_auto_fetch_ok = True


# ---------------------------------------------------------------- 登录态
def chrome_running():
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        out = (p.stdout or b"").decode("gbk", "replace").lower()
        return "chrome.exe" in out
    except Exception:  # noqa: BLE001
        return False


def ytdlp_sitepkgs():
    for base in (os.path.expanduser(r"~/AppData/Roaming/uv/tools/yt-dlp/Lib/site-packages"),
                 os.path.expanduser(r"~/.local/lib/python3/site-packages")):
        if os.path.isdir(os.path.join(base, "yt_dlp")):
            return base
    return None


def capture_cookies(browser="chrome"):
    """从本机浏览器导出登录态到 cookies.txt（需浏览器已关闭）。"""
    if chrome_running() and browser == "chrome":
        raise RuntimeError("Chrome 正在运行，Cookies 数据库被占用。请完全退出 Chrome"
                           "（含后台进程）后，再点一次「抓取登录态」。")
    sp = ytdlp_sitepkgs()
    if not sp:
        raise RuntimeError("找不到 yt-dlp 的 Python 库，无法导出登录态")
    if sp not in sys.path:
        sys.path.insert(0, sp)
    from yt_dlp.cookies import extract_cookies_from_browser

    class _L:
        def debug(self, *a): pass
        def info(self, *a): pass
        def warning(self, *a): pass
        def error(self, *a): pass

    jar = extract_cookies_from_browser(browser, logger=_L())
    jar.save(COOKIE_FILE, ignore_discard=True, ignore_expires=True)
    n = sum(1 for _ in jar)
    if n == 0:
        raise RuntimeError("没有读到任何 Cookie，请确认该浏览器登录过目标站点")
    return n


def merge_cookies(cookie_list):
    """把浏览器扩展送来的 cookie 合并进 cookies.txt（Netscape 格式）。"""
    existing = {}
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    raw = line.rstrip("\n")
                    if not raw.strip():
                        continue
                    head = raw
                    if raw.startswith("#HttpOnly_"):
                        head = raw[len("#HttpOnly_"):]
                    elif raw.startswith("#"):
                        continue
                    parts = head.split("\t")
                    if len(parts) >= 7:
                        existing[(parts[0], parts[5])] = (raw, parts)
        except Exception:  # noqa: BLE001
            existing = {}

    added = 0
    for c in cookie_list or []:
        name = c.get("name")
        if not name:
            continue
        domain = c.get("domain") or ""
        if not domain:
            continue
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        try:
            exp = int(float(c.get("expirationDate") or 0))
        except Exception:  # noqa: BLE001
            exp = 0
        if exp <= 0:
            exp = int(time.time()) + 86400 * 30
        sub = "TRUE" if domain.startswith(".") else "FALSE"
        line = "\t".join([domain, sub, path, secure, str(exp), name, c.get("value") or ""])
        if c.get("httpOnly"):
            line = "#HttpOnly_" + line
        existing[(domain, name)] = (line, None)
        added += 1

    out = ["# Netscape HTTP Cookie File", "# 由视频下载器扩展自动维护",
           ""]
    for line, _ in existing.values():
        out.append(line)
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return added


def friendly(msg):
    m = str(msg)
    low = m.lower()
    if "fresh cookies" in low:
        return "抖音的官方解析器要求「新鲜 cookie」，这条路径走不通 —— 已自动改用页面嗅探重试。"
    if "could not copy chrome cookie" in low:
        return "无法读取 Chrome Cookie —— Chrome 正在运行。请先退出 Chrome，再在页面上点「抓取登录态」。"
    if "412" in m and "bilibili" in low:
        return ("B站拦截了未登录请求（HTTP 412）。先退出 Chrome → 点「抓取登录态」，"
                "并勾选「使用浏览器登录态」后重试。")
    if "sign in to confirm" in low or "login required" in low or "需要登录" in m:
        return "该站点要求登录。请先在本机 Chrome 登录一次，再点「抓取登录态」并勾选「使用浏览器登录态」。"
    if "unsupported url" in low:
        return "这个链接不是视频页，或者该网站暂不支持。请直接粘贴视频播放页的地址。"
    if "private" in low and "video" in low:
        return "视频是私密的/需要权限，登录态不足。"
    if "timed out" in low or "timeout" in low:
        return "请求超时。若是海外站点，请勾选「走本机代理」后重试。"
    if "geo" in low and "restrict" in low:
        return "该视频有地区限制，需要代理。"
    return m[:400]


def base_cmd(url, cookies=False, proxy=False, referer=None):
    cmd = [YTDLP, "--no-playlist", "--no-warnings", "--encoding", "utf-8",
           "--no-mtime", "--socket-timeout", "25", "--retries", "3"]
    if cookies:
        if os.path.exists(COOKIE_FILE):
            cmd += ["--cookies", COOKIE_FILE]
        else:
            cmd += ["--cookies-from-browser", "chrome"]
    if proxy:
        cmd += ["--proxy", PROXY]
    if referer:
        cmd += ["--referer", referer, "--add-header", "User-Agent:" + UA]
    if FFMPEG_DIR:
        cmd += ["--ffmpeg-location", FFMPEG_DIR]
    return cmd + [url]


def _dec(b):
    b = b or b""
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def run_capture(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    return p.returncode, _dec(p.stdout), _dec(p.stderr)


def probe(url, cookies=False, proxy=False):
    real, meta = resolve_site(url)
    rc, out, err = run_capture(base_cmd(real, cookies, proxy) + ["-J"], timeout=150)
    if rc != 0 or not out.strip():
        msg = (err or out).strip().splitlines()
        raise RuntimeError(" ".join(msg[-3:])[:400] or "解析失败")
    info = json.loads(out)
    if "entries" in info:
        info = next((e for e in info["entries"] if e), {})
    heights = sorted({int(f["height"]) for f in info.get("formats") or []
                      if f.get("height")
                      and not (f.get("vcodec") == "none" and f.get("acodec") == "none")},
                     reverse=True)
    meta = meta or {}
    return {
        "title": meta.get("title") or info.get("title") or "未命名",
        "uploader": meta.get("channel") or info.get("uploader") or info.get("channel") or "",
        "duration": meta.get("duration") or info.get("duration") or 0,
        "thumb": meta.get("image") or info.get("thumbnail") or "",
        "webpage_url": url,
        "heights": heights,
        "extractor": "央视网" if meta else (info.get("extractor_key") or ""),
        "filesize": info.get("filesize") or info.get("filesize_approx") or 0,
        "resolved": real if real != url else "",
    }


def probe_sniff(url):
    """解析器认不出这个站时，改用页面嗅探：开真浏览器抓真实播放流。"""
    d = scan_page(url, wait=6.0)
    if not d.get("page_title"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=12) as f:
                html = f.read(300000).decode("utf-8", "replace")
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
            if m:
                d["page_title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:150]
        except Exception:  # noqa: BLE001
            pass
    items = d.get("items") or []
    if not items:
        return None

    def score(it):
        return (it.get("size") or 0, it.get("duration") or 0,
                1 if it.get("note") == "HLS 流" else 0)
    items = sorted(items, key=score, reverse=True)
    best = items[0]
    return {
        "title": d.get("page_title") or best.get("title") or "未命名",
        "uploader": "页面嗅探",
        "duration": best.get("duration") or 0,
        "thumb": best.get("poster") or "",
        "webpage_url": best["url"],
        "heights": [best["height"]] if best.get("height") else [],
        "extractor": "嗅探",
        "filesize": best.get("size") or 0,
        "resolved": best["url"],
        "referer": url,
        "note": ("这个站的解析器下不了，已改成直接抓页面播放流"
                 "（共抓到 %d 条，取最大的一条）。流地址有时效，点下载要趁现在。" % len(items)),
    }


# ---------------------------------------------------------------- 下载任务
JOBS = {}
JOBS_LOCK = threading.Lock()

PRESETS = {
    "best": ("bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best", "最高画质"),
    "2160": ("bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]", "2160P"),
    "1440": ("bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/best[height<=1440]", "1440P"),
    "1080": ("bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]", "1080P"),
    "720": ("bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]", "720P"),
    "480": ("bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]", "480P"),
    "audio": ("bestaudio/best", "仅音频 MP3"),
}

PROGRESS_RE = re.compile(r"__P__\s*([\d.]+)%\s*\|\s*(\S+)\s*\|\s*(\S+)")


def start_job(url, preset, cookies, proxy, title, referer=None):
    real, meta = resolve_site(url)
    if meta and meta.get("title"):
        title = meta["title"]
    jid = uuid.uuid4().hex[:12]
    jdir = os.path.join(DL_DIR, jid)
    os.makedirs(jdir, exist_ok=True)
    job = {"id": jid, "url": real, "preset": preset, "dir": jdir, "title": title,
           "referer": referer, "status": "准备中", "pct": 0.0, "speed": "", "eta": "",
           "file": "", "error": "", "t0": time.time(), "size": 0}
    with JOBS_LOCK:
        JOBS[jid] = job
    threading.Thread(target=_worker, args=(job, cookies, proxy), daemon=True).start()
    return job


def _worker(job, cookies, proxy):
    fmt, label = PRESETS[job["preset"]]
    safe = re.sub(r'[\\/:*?"<>|%\r\n\t]+', "_", job["title"] or "").strip()[:80]
    outtmpl = os.path.join(job["dir"], (safe or "%(title).70s") + ".%(ext)s")
    cmd = base_cmd(job["url"], cookies, proxy, job.get("referer"))
    cmd += ["-f", fmt, "-o", outtmpl,
            "--newline", "--progress",
            "--progress-template", "download:__P__ %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s"]
    if job["preset"] == "audio":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        cmd += ["--merge-output-format", "mp4"]
    job["status"] = "下载中"
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             creationflags=CREATE_NO_WINDOW)
        tail = []
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            m = PROGRESS_RE.search(line)
            if m:
                job["pct"] = round(float(m.group(1)), 1)
                job["speed"] = m.group(2)
                job["eta"] = m.group(3)
            else:
                tail.append(line)
                tail = tail[-12:]
        p.wait()
        if p.returncode != 0:
            job["status"] = "失败"
            job["error"] = friendly(" / ".join(t for t in tail[-4:] if t) or "yt-dlp 返回非零")
            return
        files = [os.path.join(job["dir"], f) for f in os.listdir(job["dir"])]
        files = [f for f in files if os.path.isfile(f) and not f.endswith((".part", ".ytdl"))]
        if not files:
            job["status"] = "失败"
            job["error"] = "没有产出文件"
            return
        f = max(files, key=os.path.getsize)
        job["file"] = f
        job["size"] = os.path.getsize(f)
        job["pct"] = 100.0
        job["status"] = "完成"
        job["speed"] = ""
        job["eta"] = ""
    except Exception as e:  # noqa: BLE001
        job["status"] = "失败"
        job["error"] = f"{type(e).__name__}: {e}"[:400]


def job_public(job):
    return {k: job[k] for k in ("id", "title", "status", "pct", "speed", "eta",
                                "error", "file", "size", "t0")} | {"label": PRESETS[job["preset"]][1]}


# ---------------------------------------------------------------- 整页扫描（CDP）
CHROME_PORT = 9333
PROFILE_DIR = os.path.join(BASE, "chrome-profile")
CHROME_CANDS = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
SCAN_LOCK = threading.Lock()

MEDIA_EXT = (".mp4", ".m3u8", ".flv", ".webm", ".mkv", ".mov")
SKIP_HOST_SNIPPET = ("googleadservices", "doubleclick", "googlesyndication")


def chrome_path():
    for c in CHROME_CANDS:
        if c and os.path.exists(c):
            return c
    return None


def cdp_version():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CHROME_PORT}/json/version", timeout=2) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def ensure_chrome():
    v = cdp_version()
    if v:
        return v
    exe = chrome_path()
    if not exe:
        raise RuntimeError("没找到 Chrome，整页扫描要用本机 Chrome")
    os.makedirs(PROFILE_DIR, exist_ok=True)
    subprocess.Popen([exe, f"--remote-debugging-port={CHROME_PORT}",
                      f"--user-data-dir={PROFILE_DIR}", "--no-first-run",
                      "--no-default-browser-check", "--disable-features=Translate",
                      "--window-size=1280,880", "about:blank"],
                     creationflags=CREATE_NO_WINDOW)
    for _ in range(45):
        time.sleep(0.4)
        v = cdp_version()
        if v:
            return v
    raise RuntimeError("Chrome 调试端口没起来（可能被安全软件拦了）")


class WS:
    """最小 WebSocket 客户端，够 CDP 用。"""

    def __init__(self, url, timeout=30):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP 握手失败")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise RuntimeError("CDP 拒绝连接")

    def _send_frame(self, opcode, data):
        mask = os.urandom(4)
        n = len(data)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode())

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("CDP 连接断开")
            buf += chunk
        return buf

    def recv(self):
        while True:
            b1, b2 = self._exact(2)
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            mask = self._exact(4) if (b2 & 0x80) else None
            data = self._exact(n) if n else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0x8:
                raise ConnectionError("CDP 连接被关闭")
            if opcode == 0xA:
                continue
            return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.mid = 0
        self.events = []

    def call(self, method, params=None, timeout=25):
        self.mid += 1
        mid = self.mid
        self.ws.send({"id": mid, "method": method, "params": params or {}})
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            self.events.append(msg)
        raise TimeoutError(method)

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.ws.sock], [], [], min(0.3, max(0.02, end - time.time())))
            if not r:
                continue
            try:
                self.events.append(json.loads(self.ws.recv()))
            except Exception:  # noqa: BLE001
                break


JS_VIDEOS = """JSON.stringify([...document.querySelectorAll('video')].map(v => ({
  src: v.currentSrc || v.src || (v.querySelector('source') ? v.querySelector('source').src : '') || '',
  poster: v.poster || '',
  duration: isFinite(v.duration) ? v.duration : 0,
  h: v.videoHeight || 0,
  w: v.videoWidth || 0,
  title: (v.getAttribute('title') || document.title || '').slice(0, 150)
})))"""


def quick_meta(u, timeout=8):
    try:
        rc, out, err = run_capture(base_cmd(u) + ["-J"], timeout=timeout)
        if rc != 0 or not out.strip():
            return {}
        d = json.loads(out)
        hs = sorted({int(f["height"]) for f in d.get("formats") or []
                     if f.get("height") and not (f.get("vcodec") == "none" and f.get("acodec") == "none")},
                    reverse=True)
        return {"title": d.get("title") or "", "duration": d.get("duration") or 0,
                "heights": hs, "thumb": d.get("thumbnail") or ""}
    except Exception:  # noqa: BLE001
        return {}


def head_size(u, referer):
    try:
        req = urllib.request.Request(u, method="HEAD",
                                     headers={"User-Agent": UA, "Referer": referer or u})
        with urllib.request.urlopen(req, timeout=8) as f:
            return int(f.headers.get("Content-Length") or 0)
    except Exception:  # noqa: BLE001
        return 0


def scan_page(url, wait=5.0):
    """真开一个标签页跑起来，抓 <video> 和网络请求里的媒体地址。"""
    if not SCAN_LOCK.acquire(blocking=False):
        raise RuntimeError("上一轮扫描还没结束，等它跑完再点")
    ws = None
    try:
        ensure_chrome()
        req = urllib.request.Request(
            f"http://127.0.0.1:{CHROME_PORT}/json/new?{urllib.parse.quote('about:blank', safe='')}",
            method="PUT")
        with urllib.request.urlopen(req, timeout=20) as f:
            tab = json.load(f)
        ws = WS(tab["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Network.enable", {"maxTotalBufferSize": 64 * 1024 * 1024})
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.navigate", {"url": url})
        cdp.pump(wait)

        # 试着让播放器动起来，很多站要 play() 之后才拉流
        try:
            cdp.call("Runtime.evaluate", {"expression": """
                [...document.querySelectorAll('video')].forEach(v => { try { v.muted = true; v.play(); } catch(e){} });
            """, "returnByValue": True}, timeout=4)
            cdp.pump(1.5)
        except Exception:  # noqa: BLE001
            pass

        dom = []
        try:
            r = cdp.call("Runtime.evaluate", {"expression": JS_VIDEOS, "returnByValue": True}, timeout=6)
            dom = json.loads((r.get("result") or {}).get("value") or "[]")
        except Exception:  # noqa: BLE001
            dom = []

        page_title = ""
        try:
            r = cdp.call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True}, timeout=5)
            page_title = (r.get("result") or {}).get("value") or ""
        except Exception:  # noqa: BLE001
            pass

        try:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{CHROME_PORT}/json/close/{tab['id']}"),
                timeout=8)
        except Exception:  # noqa: BLE001
            pass

        items, seen = [], set()

        def add(u, mime, note="", h=0, duration=0, poster="", title=""):
            if not u or u in seen:
                return
            low = u.lower()
            if low.startswith(("blob:", "data:")):
                return
            if any(s in low for s in SKIP_HOST_SNIPPET):
                return
            seen.add(u)
            items.append({"url": u, "mime": mime or "", "note": note, "height": h,
                          "duration": duration, "poster": poster, "title": title})

        for v in dom:
            add(v.get("src", ""), "video/*", "页面内播放器", v.get("h", 0),
                v.get("duration", 0), v.get("poster", ""), v.get("title", ""))

        for ev in cdp.events:
            if ev.get("method") != "Network.responseReceived":
                continue
            p = ev.get("params") or {}
            resp = p.get("response") or {}
            u = resp.get("url") or ""
            mime = (resp.get("mimeType") or "").lower()
            low = u.lower()
            if low.endswith(".ts") or "/seg-" in low or "seg.ts" in low:
                continue
            if any(k in low for k in MEDIA_EXT) or mime.startswith("video/"):
                kind = "HLS 流" if (".m3u8" in low or "mpegurl" in mime) else "视频文件"
                add(u, mime, kind)

        # 补元数据：m3u8 用 yt-dlp 探，mp4 用 HEAD 拿体积
        todo = [it for it in items if it["note"] != "页面内播放器"][:4]
        box = {}

        def probe_one(idx, it):
            u = it["url"]
            if ".m3u8" in u.lower() or "mpegurl" in it["mime"]:
                box[idx] = quick_meta(u, timeout=9)
            else:
                box[idx] = {"size": head_size(u, url)}
        threads = [threading.Thread(target=probe_one, args=(i, it), daemon=True)
                   for i, it in enumerate(todo)]
        for t in threads:
            t.start()
        deadline = time.time() + 5
        for t in threads:
            t.join(max(0.05, deadline - time.time()))

        for i, it in enumerate(todo):
            m = box.get(i) or {}
            if m.get("duration"):
                it["duration"] = m["duration"]
            if m.get("heights"):
                it["height"] = m["heights"][0]
            if m.get("size"):
                it["size"] = m["size"]
            if not it["title"] and m.get("title") and m["title"] != "main":
                it["title"] = m["title"]

        items.sort(key=lambda x: (x["note"] == "页面内播放器", -(x.get("size") or 0)))
        hint = ""
        if not items:
            low = (page_title or "").lower()
            if any(k in low for k in ("出错", "404", "error", "访问异常", "验证")):
                hint = ("页面本身没打开成功（标题：" + (page_title or "空") +
                        "）。这个站点可能在拦这台机器的 IP，或者要求登录/人机验证。")
            else:
                hint = ("页面里没有可抓的视频请求。可能是：视频要手动点播放才加载、"
                        "需要登录、或者是纯图片/长文页面。")
        return {"page": url, "page_title": page_title, "count": len(items),
                "items": items, "hint": hint}
    finally:
        if ws:
            ws.close()
        SCAN_LOCK.release()


# ---------------------------------------------------------------- 页面
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>视频下载器</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0b0b12; --card:#14141f; --card2:#101019; --line:#24243a;
  --pri:#7c5cff; --pri2:#22d3ee; --ok:#54d38a; --bad:#ff6b8a;
  --txt:#eaeaf5; --dim:#9a9ab5; --dim2:#6b6b85;
}
body{
  background:
    radial-gradient(900px 500px at 12% -8%, rgba(124,92,255,.20), transparent 60%),
    radial-gradient(760px 420px at 92% 4%, rgba(34,211,238,.12), transparent 62%),
    var(--bg);
  color:var(--txt); font:14px/1.6 "Microsoft YaHei","PingFang SC",sans-serif;
  min-height:100vh; padding:26px 18px 60px;
}
.wrap{max-width:940px;margin:0 auto}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:10px;font-size:19px;font-weight:700;letter-spacing:.02em}
.brand em{font-style:normal;font-size:11px;color:var(--dim2);font-weight:400;letter-spacing:.1em}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 12px var(--ok);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.hud{display:flex;gap:16px;font-size:11px;color:var(--dim2);letter-spacing:.12em;font-family:Consolas,monospace}
.card{
  background:linear-gradient(160deg,rgba(21,21,34,.95),rgba(13,13,21,.92));
  border:1px solid var(--line); border-radius:14px; padding:20px; margin-bottom:16px;
  backdrop-filter:blur(12px); position:relative;
}
.card:before{content:"";position:absolute;inset:0 0 auto 0;height:1px;border-radius:14px 14px 0 0;
  background:linear-gradient(90deg,transparent,rgba(124,92,255,.55),transparent)}
.row{display:flex;gap:10px}
input[type=text]{
  flex:1;min-width:0;background:#0b0b14;border:1px solid var(--line);color:var(--txt);
  border-radius:10px;padding:13px 15px;font-size:14px;outline:none;transition:.2s
}
input[type=text]:focus{border-color:rgba(124,92,255,.65);box-shadow:0 0 0 3px rgba(124,92,255,.13)}
input[type=text]::placeholder{color:#55556e}
button{
  border:1px solid var(--line);background:#1b1b2b;color:var(--txt);border-radius:10px;
  padding:12px 18px;font-size:13.5px;cursor:pointer;font-family:inherit;transition:.18s;white-space:nowrap
}
button:hover{background:#23233a;border-color:rgba(124,92,255,.5);transform:translateY(-1px)}
button:disabled{opacity:.45;cursor:not-allowed;transform:none}
button.primary{background:linear-gradient(135deg,var(--pri),#5b3ce0);border-color:transparent;font-weight:600;
  box-shadow:0 6px 20px rgba(124,92,255,.28)}
button.primary:hover{box-shadow:0 8px 26px rgba(124,92,255,.42)}
button.ghost{background:transparent}
button.scan{border-color:rgba(34,211,238,.45);color:#7fe6f5;background:rgba(34,211,238,.08)}
button.scan:hover{background:rgba(34,211,238,.18);border-color:rgba(34,211,238,.7)}
.cnt{color:var(--dim2);font-family:Consolas,monospace;font-size:11px;margin-left:8px;letter-spacing:0}
.sitem{display:flex;align-items:center;gap:12px;padding:11px 13px;border:1px solid var(--line);
  border-radius:10px;margin-bottom:9px;background:var(--card2)}
.sinfo{flex:1;min-width:0}
.sname{font-size:13px;margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.surl{font-size:11px;line-height:1.7;color:var(--dim2);font-family:Consolas,monospace;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.surl b{color:#7fe6f5;font-weight:500}
.sitem button{padding:8px 15px;font-size:12px;border-radius:8px;flex:0 0 auto}
.opts{display:flex;flex-wrap:wrap;gap:18px;margin-top:14px;font-size:12.5px;color:var(--dim)}
.opts label{display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none}
.opts input{accent-color:var(--pri);width:14px;height:14px}
.ckline{margin-top:9px;font-size:12px;color:var(--dim2)}
.ckline b{color:var(--ok);font-family:Consolas,monospace}
.ckline a{color:var(--pri2);cursor:pointer;text-decoration:none;border-bottom:1px dashed rgba(34,211,238,.4)}
.ckline a:hover{color:#5ff0ff}
.msg{margin-top:12px;font-size:12.5px;min-height:0;color:var(--dim)}
.msg.err{color:var(--bad)}
.msg.busy{color:var(--pri2)}
.media{display:flex;gap:18px}
#thumb{width:210px;height:118px;object-fit:cover;border-radius:10px;background:#0b0b14;border:1px solid var(--line);flex:0 0 auto}
.meta{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px}
.meta h2{font-size:16px;font-weight:600;line-height:1.45;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.meta p{font-size:12.5px;color:var(--dim2)}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:4px}
.chip{font-size:11px;letter-spacing:.06em;padding:3px 9px;border-radius:20px;
  border:1px solid var(--line);color:var(--dim);font-family:Consolas,monospace}
.chip.hot{border-color:rgba(84,211,138,.4);color:var(--ok);background:rgba(84,211,138,.07)}
.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}
h3{font-size:12px;letter-spacing:.14em;color:var(--dim2);margin-bottom:14px;font-weight:500;text-transform:uppercase}
.job{background:var(--card2);border:1px solid var(--line);border-radius:11px;padding:13px 15px;margin-bottom:10px}
.job .jt{display:flex;justify-content:space-between;gap:12px;font-size:13px;margin-bottom:9px}
.job .jt span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job .jt span:last-child{color:var(--dim2);font-family:Consolas,monospace;font-size:11.5px;flex:0 0 auto}
.bar{height:5px;background:#0b0b14;border-radius:4px;overflow:hidden}
.bar i{display:block;height:100%;width:0;border-radius:4px;
  background:linear-gradient(90deg,var(--pri),var(--pri2));transition:width .35s ease;
  box-shadow:0 0 10px rgba(124,92,255,.6)}
.bar.done i{background:linear-gradient(90deg,#3aa96b,var(--ok));box-shadow:0 0 10px rgba(84,211,138,.5)}
.bar.fail i{background:var(--bad);box-shadow:none}
.jstate{display:flex;justify-content:space-between;gap:10px;margin-top:8px;font-size:11.5px;color:var(--dim2);font-family:Consolas,monospace}
.jact{display:flex;gap:8px;margin-top:10px}
.jact button{padding:7px 13px;font-size:12px;border-radius:8px}
.empty{color:var(--dim2);font-size:12.5px;padding:6px 0}
.hidden{display:none!important}
.bmktip{font-size:12.5px;color:var(--dim2);line-height:1.9;margin-bottom:12px}
.bookmark{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.bookmark span{font-size:12px;color:var(--dim2)}
.bmk{display:inline-block;padding:8px 15px;border-radius:9px;text-decoration:none;
  border:1px dashed rgba(124,92,255,.6);color:var(--txt);font-size:12.5px;
  background:rgba(124,92,255,.12);transition:.18s}
.bmk:hover{background:rgba(124,92,255,.24)}
.spin{display:inline-block;width:11px;height:11px;border:2px solid rgba(34,211,238,.25);
  border-top-color:var(--pri2);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-1px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand"><span class="dot"></span>视频下载器 <em>LOCAL v1.0</em></div>
    <div class="hud"><span>ENGINE ONLINE</span><span id="clock">--:--:--</span></div>
  </header>

  <section class="card">
    <div class="row">
      <input type="text" id="url" placeholder="粘贴视频页面链接：抖音 / B站 / 小红书 / YouTube / X …" autocomplete="off">
      <button class="ghost" id="paste">粘贴</button>
      <button class="primary" id="probe">解析</button>
      <button class="scan" id="scan">整页扫描</button>
    </div>
    <div class="opts">
      <label><input type="checkbox" id="ck"> 使用浏览器登录态（抖音 / 小红书 / B站）</label>
      <label><input type="checkbox" id="px"> 走本机代理 127.0.0.1:7897</label>
    </div>
    <div class="ckline" id="ckline">登录态：检测中…</div>
    <div class="msg" id="msg"></div>
  </section>

  <section class="card hidden" id="result">
    <div class="media">
      <img id="thumb" alt="">
      <div class="meta">
        <h2 id="title"></h2>
        <p id="sub"></p>
        <div class="chips" id="heights"></div>
      </div>
    </div>
    <div class="actions" id="actions">
      <button class="primary" data-preset="best">最高画质</button>
      <button data-preset="1080">1080P</button>
      <button data-preset="720">720P</button>
      <button data-preset="480">480P</button>
      <button class="ghost" data-preset="audio">仅音频 MP3</button>
    </div>
  </section>

  <section class="card">
    <h3>下载任务</h3>
    <div id="jobs"><div class="empty">还没有任务，先在上面粘一个链接。</div></div>
  </section>

  <section class="card hidden" id="scanres">
    <h3>本页视频 <span class="cnt" id="scancount"></span></h3>
    <div id="scanlist"></div>
    <p class="bmktip" style="margin-top:12px;margin-bottom:0">扫描用的是工具自带的 Chrome 窗口（独立配置，存在工具目录）。要下抖音 / 小红书这类要登录的站，在那个窗口里登录一次，之后每次扫描都带着登录态。</p>
  </section>

  <section class="card">
    <h3>书签快捷方式</h3>
    <p class="bmktip">把下面这个按钮拖到浏览器书签栏。以后在任意视频页面点它，当前页面会自动送进本工具解析，不用复制粘贴。</p>
    <div class="bookmark">
      <a class="bmk" id="bmk" href="javascript:(()=>{location.href='http://127.0.0.1:8791/?u='+encodeURIComponent(location.href)})()">⬇ 下载本页视频</a>
      <span>← 拖我到书签栏</span>
    </div>
  </section>
</div>

<script>
const $ = s => document.querySelector(s);
let CUR = null;

function tickClock(){
  const d = new Date();
  $('#clock').textContent = d.toLocaleTimeString('zh-CN',{hour12:false});
}
tickClock(); setInterval(tickClock, 1000);

function setMsg(t, cls){
  const m = $('#msg'); m.textContent = t || ''; m.className = 'msg' + (cls?' '+cls:'');
}
async function ckState(){
  const el = $('#ckline');
  try {
    const d = await (await fetch('/api/cookies/status')).json();
    if(d.imported){
      el.innerHTML = `登录态已导入 <b>${d.count}</b> 条 · <a id="ckgrab">重新抓取</a>`;
      if(!window.__cked){ $('#ck').checked = true; window.__cked = true; }
    } else {
      el.innerHTML = `未导入登录态 · <a id="ckgrab">抓取登录态</a>` + (d.chrome_running ? '（需先完全退出 Chrome）' : '');
    }
    const g = document.getElementById('ckgrab');
    if(g) g.onclick = grab;
  } catch(e){ el.textContent = ''; }
}

async function grab(){
  setMsg('', '');
  $('#msg').innerHTML = '<span class="spin"></span>正在从 Chrome 读取登录态…';
  $('#msg').className = 'msg busy';
  try {
    const r = await fetch('/api/cookies/capture', {method:'POST'});
    const d = await r.json();
    if(!r.ok) throw new Error(d.error || '抓取失败');
    setMsg('登录态已导入 ' + d.count + ' 条，现在可以解析抖音 / 小红书 / B站 了', '');
    $('#ck').checked = true; window.__cked = true;
    ckState();
  } catch(e){
    setMsg(e.message || String(e), 'err');
  }
}
ckState();

function fmtDur(s){
  s = Math.round(s||0); if(!s) return '';
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return (h?h+':':'') + String(m).padStart(2,'0') + ':' + String(x).padStart(2,'0');
}
function fmtSize(b){
  if(!b) return '';
  const u = ['B','KB','MB','GB']; let i = 0;
  while(b >= 1024 && i < u.length-1){ b/=1024; i++; }
  return b.toFixed(b<10?1:0) + u[i];
}
function fmtTime(ts){
  return new Date(ts*1000).toLocaleTimeString('zh-CN',{hour12:false});
}

$('#paste').onclick = async () => {
  try {
    const t = await navigator.clipboard.readText();
    if(t){ $('#url').value = t.trim(); setMsg('已粘贴，点「解析」', 'busy'); }
    else setMsg('剪贴板是空的');
  } catch(e){ setMsg('浏览器不允许读剪贴板，请手动 Ctrl+V', 'err'); }
};

$('#url').addEventListener('keydown', e => { if(e.key === 'Enter') doProbe(); });

let SCAN = null, SCANPAGE = '';
function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

$('#scan').onclick = doScan;

async function doScan(){
  const url = $('#url').value.trim();
  if(!url){ setMsg('先粘贴一个页面链接', 'err'); return; }
  if(!/^https?:\/\//i.test(url)){ setMsg('链接要以 http:// 或 https:// 开头', 'err'); return; }
  $('#scan').disabled = true;
  const t0 = Date.now();
  $('#msg').className = 'msg busy';
  $('#msg').innerHTML = '<span class="spin"></span>正在打开页面抓取视频… 0s';
  const tick = setInterval(() => {
    const s = Math.round((Date.now()-t0)/1000);
    $('#msg').innerHTML = '<span class="spin"></span>正在打开页面抓取视频… ' + s + 's'
      + (s > 4 ? '（会真的把页面开一遍，稍等）' : '');
  }, 400);
  try {
    const r = await fetch('/api/scan', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url: url, wait: 5})});
    const d = await r.json();
    clearInterval(tick);
    if(!r.ok) throw new Error(d.error || '扫描失败');
    SCAN = d; SCANPAGE = url;
    renderScan(d);
    const sec = ((Date.now()-t0)/1000).toFixed(1);
    setMsg('扫完了：找到 ' + d.count + ' 个视频，用时 ' + sec + ' 秒 —— 选一个点「下载」');
  } catch(e) {
    clearInterval(tick);
    setMsg(e.message || String(e), 'err');
  } finally {
    $('#scan').disabled = false;
  }
}

function renderScan(d){
  const box = $('#scanres');
  box.classList.remove('hidden');
  $('#scancount').textContent = '共 ' + d.count + ' 个 · ' + (d.page_title || '');
  if(!d.items || !d.items.length){
    $('#scanlist').innerHTML = '<div class="empty">' + esc(d.hint || '这个页面没抓到视频地址。') + '</div>';
    return;
  }
  $('#scanlist').innerHTML = d.items.map((it, i) => {
    let host = '';
    try { host = new URL(it.url).hostname; } catch(e){}
    const tail = it.url.length > 118 ? it.url.slice(0, 118) + '…' : it.url;
    const bits = [
      it.note ? '<b>' + esc(it.note) + '</b>' : '',
      it.height ? it.height + 'P' : '',
      it.duration ? fmtDur(it.duration) : '',
      it.size ? fmtSize(it.size) : ''
    ].filter(Boolean).join(' · ');
    return '<div class="sitem">'
      + '<div class="sinfo">'
      + '<div class="sname">' + esc((it.title || host || ('视频 ' + (i+1))).slice(0, 90)) + '</div>'
      + '<div class="surl">' + bits + '<br>' + esc(tail) + '</div>'
      + '</div>'
      + '<button class="primary" data-i="' + i + '">下载</button>'
      + '</div>';
  }).join('');
  document.querySelectorAll('#scanlist button').forEach(b => {
    b.onclick = () => dlScan(parseInt(b.dataset.i, 10), b);
  });
}

async function dlScan(i, btn){
  const it = SCAN.items[i];
  if(btn){ btn.disabled = true; btn.textContent = '已加入'; }
  try {
    const r = await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url: it.url, preset:'best', referer: SCANPAGE,
        cookies: $('#ck').checked, proxy: $('#px').checked, title: it.title || '视频'})});
    const d = await r.json();
    if(!r.ok) throw new Error(d.error || '启动失败');
    setMsg('已加入下载：' + (it.title || '视频') + ' —— 进度看下面的任务卡');
    await refresh();
  } catch(e) {
    setMsg(e.message || String(e), 'err');
    if(btn){ btn.disabled = false; btn.textContent = '下载'; }
  }
}

$('#probe').onclick = doProbe;

async function doProbe(){
  const url = $('#url').value.trim();
  if(!url){ setMsg('先粘贴一个视频链接', 'err'); return; }
  if(!/^https?:\/\//i.test(url)){ setMsg('链接要以 http:// 或 https:// 开头', 'err'); return; }
  if(!$('#px').checked && /youtube\.com|youtu\.be|x\.com|twitter\.com|facebook\.com|instagram\.com|vimeo\.com|tiktok\.com/i.test(url)){
    $('#px').checked = true;
  }
  $('#probe').disabled = true;
  setMsg('', '');
  $('#msg').innerHTML = '<span class="spin"></span>正在读取视频信息…';
  $('#msg').className = 'msg busy';
  const t0 = Date.now();
  const timer = setInterval(() => {
    const s = Math.round((Date.now()-t0)/1000);
    if(s >= 2) $('#msg').innerHTML = '<span class="spin"></span>正在读取视频信息… ' + s + 's';
  }, 500);
  try {
    const r = await fetch('/api/probe', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url, cookies:$('#ck').checked, proxy:$('#px').checked})
    });
    const d = await r.json();
    clearInterval(timer);
    if(!r.ok) throw new Error(d.error || '解析失败');
    CUR = d;
    $('#title').textContent = d.title;
    const bits = [d.uploader, d.extractor, fmtDur(d.duration)].filter(Boolean);
    $('#sub').textContent = bits.join('  ·  ');
    if(d.thumb){ $('#thumb').src = '/api/thumb?u=' + encodeURIComponent(d.thumb); $('#thumb').classList.remove('hidden'); }
    else $('#thumb').classList.add('hidden');
    const hs = (d.heights||[]).slice(0,9);
    $('#heights').innerHTML = hs.length
      ? hs.map((h,i)=>`<span class="chip${i===0?' hot':''}">${h}P</span>`).join('')
      : '<span class="chip">单一清晰度</span>';
    $('#result').classList.remove('hidden');
    const sec = ((Date.now()-t0)/1000).toFixed(1);
    setMsg(d.note ? ('解析完成 ' + sec + ' 秒 · ' + d.note)
                  : ('解析完成，用时 ' + sec + ' 秒 —— 选一个清晰度下载'), d.note ? 'busy' : '');
  } catch(e){
    clearInterval(timer);
    $('#result').classList.add('hidden');
    setMsg(e.message || String(e), 'err');
  } finally {
    $('#probe').disabled = false;
  }
}

document.querySelectorAll('#actions button').forEach(b => {
  b.onclick = async () => {
    if(!CUR) return;
    const preset = b.dataset.preset;
    b.disabled = true;
    try {
      const r = await fetch('/api/start', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({url:CUR.webpage_url || $('#url').value.trim(), preset,
          cookies:$('#ck').checked, proxy:$('#px').checked, title:CUR.title,
          referer: CUR.referer || ''})
      });
      const d = await r.json();
      if(!r.ok) throw new Error(d.error || '启动失败');
      await refresh();
    } catch(e){
      setMsg(e.message || String(e), 'err');
    } finally {
      b.disabled = false;
    }
  };
});

let JOBS = [];

function renderJobs(){
  const box = $('#jobs');
  if(!JOBS.length){ box.innerHTML = '<div class="empty">还没有任务，先在上面粘一个链接。</div>'; return; }
  box.innerHTML = JOBS.map(j => {
    const cls = j.status === '完成' ? 'done' : (j.status === '失败' ? 'fail' : '');
    const pct = j.status === '完成' ? 100 : j.pct;
    const info = j.status === '失败'
      ? `<span style="color:#ff6b8a">${(j.error||'').slice(0,90)}</span>`
      : j.status === '完成'
        ? `${fmtSize(j.size)} · 完成于 ${fmtTime(j.t0)}`
        : `${j.status}${j.speed?' · '+j.speed:''}${j.eta?' · 剩余 '+j.eta:''}`;
    const act = j.status === '完成'
      ? `<div class="jact">
           <button class="primary" onclick="dl('${j.id}')">下载到本机</button>
           <button class="ghost" onclick="reveal('${j.id}')">打开文件夹</button>
         </div>` : '';
    return `<div class="job">
      <div class="jt"><span>${j.preset_label ? '['+j.preset_label+'] ' : ''}${j.title||''}</span><span>${pct.toFixed(0)}%</span></div>
      <div class="bar ${cls}"><i style="width:${pct}%"></i></div>
      <div class="jstate"><span>${info}</span><span>${j.status==='完成'?'OK':''}</span></div>
      ${act}
    </div>`;
  }).join('');
}

function dl(id){ location.href = '/api/file?id=' + id; }
async function reveal(id){
  try { await fetch('/api/reveal?id=' + id, {method:'POST'}); } catch(e){}
}

async function refresh(){
  try {
    const r = await fetch('/api/jobs');
    const d = await r.json();
    JOBS = d.jobs || [];
    renderJobs();
  } catch(e){}
}
refresh();

const _u = new URLSearchParams(location.search).get('u');
if(_u){ $('#url').value = _u; setTimeout(doProbe, 150); }

setInterval(() => {
  if(JOBS.some(j => j.status !== '完成' && j.status !== '失败')) refresh();
}, 800);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "LocalVideoDL/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        for enc in ("utf-8", "gbk"):
            try:
                return json.loads(raw.decode(enc))
            except Exception:
                continue
        return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = [job_public(j) for j in JOBS.values()]
            jobs.sort(key=lambda j: -j["t0"])
            self._json({"jobs": jobs})
            return
        if path == "/api/thumb":
            src = (q.get("u") or [""])[0]
            if not re.match(r"^https?://", src):
                self._send(404, b"", "text/plain")
                return
            try:
                req = urllib.request.Request(src, headers={"User-Agent": UA,
                                                          "Referer": "https://app.cctv.com/"})
                with urllib.request.urlopen(req, timeout=15) as f:
                    data = f.read(8 * 1024 * 1024)
                    ct = f.headers.get("Content-Type") or "image/jpeg"
                self._send(200, data, ct)
            except Exception:  # noqa: BLE001
                self._send(404, b"", "text/plain")
            return
        if path == "/api/cookies/status":
            has = os.path.exists(COOKIE_FILE)
            n = 0
            if has:
                try:
                    with open(COOKIE_FILE, "r", encoding="utf-8", errors="replace") as fh:
                        n = sum(1 for line in fh if line.strip() and not line.startswith("#"))
                except Exception:  # noqa: BLE001
                    n = 0
            self._json({"imported": has, "count": n, "chrome_running": chrome_running()})
            return
        if path == "/api/file":
            jid = (q.get("id") or [""])[0]
            job = JOBS.get(jid)
            if not job or job["status"] != "完成":
                self._json({"error": "任务不存在或未完成"}, 404)
                return
            f = job["file"]
            size = os.path.getsize(f)
            name = os.path.basename(f)
            fh = open(f, "rb")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition",
                                 "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
                self.end_headers()
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass
            finally:
                fh.close()
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/cookies/capture":
            try:
                n = capture_cookies("chrome")
                self._json({"ok": True, "count": n})
            except Exception as e:  # noqa: BLE001
                self._json({"error": friendly(e)}, 500)
            return
        if u.path == "/api/probe":
            d = self._body()
            url = (d.get("url") or "").strip()
            if not re.match(r"^https?://", url):
                self._json({"error": "链接无效"}, 400)
                return
            if not YTDLP:
                self._json({"error": "找不到 yt-dlp，请先安装"}, 500)
                return
            try:
                info = probe(url, bool(d.get("cookies")), bool(d.get("proxy")))
                self._json(info)
            except subprocess.TimeoutExpired:
                self._json({"error": "解析超时（150 秒），该站点可能需要登录态或代理"}, 504)
            except Exception as e:  # noqa: BLE001
                first_err = friendly(e)
                # 解析器不认这个站（典型：抖音要新鲜 cookie）→ 改用页面嗅探
                try:
                    sniffed = probe_sniff(url)
                except Exception as e2:  # noqa: BLE001
                    sniffed = None
                    sniff_err = friendly(e2)
                if sniffed:
                    sniffed["note"] = (sniffed.get("note", "") +
                                       "（原本的错误：%s）" % first_err)[:300]
                    self._json(sniffed)
                else:
                    self._json({"error": first_err}, 500)
            return
        if u.path == "/api/dl":
            d = self._body()
            url = (d.get("url") or "").strip()
            if not re.match(r"^https?://", url):
                self._json({"error": "链接无效"}, 400)
                return
            n = merge_cookies(d.get("cookies") or [])
            job = start_job(url, d.get("preset") or "best", True, bool(d.get("proxy")),
                            d.get("title") or url, d.get("referer"))
            self._json({"id": job["id"], "cookie_n": n})
            return
        if u.path == "/api/scan":
            d = self._body()
            url = (d.get("url") or "").strip()
            if not re.match(r"^https?://", url):
                self._json({"error": "链接无效"}, 400)
                return
            try:
                wait = float(d.get("wait") or 5)
            except Exception:  # noqa: BLE001
                wait = 5.0
            try:
                self._json(scan_page(url, max(3.0, min(15.0, wait))))
            except Exception as e:  # noqa: BLE001
                self._json({"error": friendly(e)}, 500)
            return
        if u.path == "/api/start":
            d = self._body()
            url = (d.get("url") or "").strip()
            preset = d.get("preset") or "best"
            if preset not in PRESETS:
                self._json({"error": "不支持的清晰度"}, 400)
                return
            if not re.match(r"^https?://", url):
                self._json({"error": "链接无效"}, 400)
                return
            default_title = d.get("title") or url
            job = start_job(url, preset, bool(d.get("cookies")), bool(d.get("proxy")),
                            default_title, d.get("referer"))
            self._json({"id": job["id"]})
            return
        if u.path == "/api/reveal":
            d = self._body()
            q = urllib.parse.parse_qs(u.query)
            jid = (q.get("id") or [""])[0] or (d.get("id") or "")
            job = JOBS.get(jid)
            if job and os.path.isdir(job["dir"]):
                try:
                    subprocess.Popen(["explorer", job["dir"]])
                except Exception:  # noqa: BLE001
                    pass
                self._json({"ok": True})
                return
            self._json({"error": "目录不存在"}, 404)
            return
        self._json({"error": "not found"}, 404)


def main():
    if not ensure_engine():
        print("=" * 52)
        print("  缺少下载引擎 yt-dlp，自动下载也没成功。")
        print("  办法一：联网后重开一次（工具会自己下）")
        print("  办法二：手动下载 yt-dlp.exe 放到本目录的 tools/ 里")
        print("  下载地址：https://github.com/yt-dlp/yt-dlp/releases/latest")
        print("=" * 52)
        input("按回车退出…")
        sys.exit(1)
    url = f"http://127.0.0.1:{PORT}"
    import socket
    with socket.socket() as s:
        s.settimeout(1.5)
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"程序已经在运行：{url}")
            if os.environ.get("OPEN_BROWSER") == "1":
                import webbrowser
                webbrowser.open(url)
            return
    print("=" * 46)
    print("  视频下载器 已启动")
    print(f"  地址: {url}")
    print(f"  引擎: {YTDLP}")
    print(f"  转码: {FFMPEG_DIR or '未找到 ffmpeg（HLS 合成会失败，直链 mp4 不受影响）'}")
    print(f"  保存: {DL_DIR}")
    print("  关闭这个窗口即停止服务")
    print("=" * 46)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    if os.environ.get("OPEN_BROWSER") == "1":
        import threading as _t
        import webbrowser
        _t.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
