---
name: local-video-downloader
description: Use when deploying a local yt-dlp web downloader.
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [windows, macos, linux]
metadata:
  hermes:
    tags: [yt-dlp, downloader, local-webapp, video, cctv, chrome-extension, portable]
    category: software-development
---

# 本地视频下载器工作流（网页版 + Chrome 扩展 + 便携分发）

一句话：**浏览器只负责识别，下载交给本地 yt-dlp**。这套组合能覆盖绝大多数网页视频，
包括央视这种要单独取流的、抖音这种官方解析器要「新鲜 cookie」的。

仓库：https://github.com/yongkangz531-hash/video-downloader
便携包：Release 里的 `video-downloader-portable-v1.0.0.zip`（内置 Python + yt-dlp）

## When to Use

- 用户要「下载网页里的视频」，不装收费软件、不注册账号、不传云端
- 要能「刷到什么下什么」（Chrome 扩展），也要能「粘链接批量挑」（网页版）
- 要换台电脑就能用（便携包）
- 目标站点含抖音、小红书、B站、央视、YouTube

## 一、在新机器上跑起来（三种，按条件选）

**A. 便携版（对方机器没 Python）**
下 Release 的 zip → 解压任意目录 → 双击 `启动下载器.bat`。包里带 `python\python.exe` 和 `tools\yt-dlp.exe`，什么都不用装。

**B. 源码（已装 Python 3.9+）**
`python app.py`。首次运行若没有 yt-dlp，`ensure_engine()` 会自动下到 `tools/yt-dlp.exe`（直连失败自动走 `VD_PROXY`，默认 `http://127.0.0.1:7897`）。

**C. 命令行**
`yt-dlp <url>`；只有网页交互需求才需要本工具。

扩展装一次：`chrome://extensions` → 开发者模式 → 加载已解压 → 选 `extension`。

## 二、三个入口，共用一套下载内核

| 入口 | 适用 | 路径 |
|---|---|---|
| 单条解析 | 粘贴视频页链接 | `/api/probe` → `/api/start` |
| 整页扫描 | 一页多个视频 | `/api/scan` → 列表挑一条 |
| Chrome 扩展 | 边刷边下 | 扩展嗅探 → `/api/dl`（附 cookie） |

三者最终都落到 `start_job()`：`yt-dlp -f <preset> -o <jobdir>/标题.%(ext)s`。

## 三、关键技术决策（这几条是这套东西能成立的原因）

1. **解析失败自动回退嗅探**：`probe()` 抛错就 `probe_sniff()` —— 用 CDP 真开页面，抓真实播放流，把结果伪装成正常 probe 响应（`webpage_url`=流地址、`referer`=原页面）。抖音 15 秒拿到标题 + 7.1MB 下载成功，全靠这条。
2. **嗅探按 Content-Type 认媒体，不靠 URL 后缀**：抖音的流地址没有 `.mp4`。MV3 用 `chrome.webRequest.onHeadersReceived` + `['responseHeaders']`，判 `video/*`、`audio/*`、`mpegurl`、`dash+xml`。
3. **cookie 由扩展提供**：`chrome.cookies.getAll({url})` 拿到实时 cookie，POST 给 helper 写成 Netscape `cookies.txt` 再 `--cookies`。比 `--cookies-from-browser` 可靠（不受 Chrome 运行锁影响）。
4. **每个任务独立子目录**：产物用 `os.listdir(jobdir)` 取最大文件，绝不靠解析日志猜文件名。
5. **文件名用任务标题清洗**：`re.sub(r'[\\/:*?"<>|%]+','_',title)`，否则 HLS 站点产物一律叫 `main.mp4`。
6. **央视单独适配**：URL 取 `guid` → `https://vdn.apps.cntv.cn/api/getHttpVideoInfo.do?pid=<guid>` → 取 `hls_url`，标题/封面/时长/频道同源拿。

## 四、坑（全实测）

- **Chrome 137+ 不能命令行装扩展**：`--load-extension` 已移除，`--disable-features=DisableLoadExtensionCommandLineSwitch` 在 Chrome 156 上实测也无效。只能手动加载，交付时配一个 bat 同时打开扩展页和目录。
- **扩展跨域调本地服务必须带 CORS**：`Access-Control-Allow-Origin: *` + 处理 `OPTIONS` 预检，否则 fetch 直接失败。
- **bookmarklet 必须写在 HTML 属性里**：`a.href = "javascript:..."` 会被 percent-encode（`'`→`%27`）破坏语法。
- **Windows tasklist 输出是 GBK**：`subprocess.run(..., text=True)` 在 UTF-8 mode 下抛 UnicodeDecodeError、stdout 变 None。用 `capture_output=True` 拿 bytes 再 `decode('gbk','replace')`。
- **Chrome 运行时读不到 cookie 数据库**：`Could not copy Chrome cookie database`；`CreateFileW` 带共享位也是 `ERROR_SHARING_VIOLATION`(32)。所以走扩展那条路。
- **B站 412 是 IP 级风控**：curl 首页 200、Chrome 打开视频页也是「出错啦」，调 UA/Referer 没用，别浪费时间。
- **m3u8 的 format 常 vcodec=None 且 acodec=None**：清晰度过滤用 `not (vcodec=='none' and acodec=='none')`，否则列表永远空。
- **缩略图防盗链**：`<img src=远程图>` 是黑框，走后端 `/api/thumb?u=` 带 UA+Referer。
- **terminal 起的常驻服务会随会话被杀**：要脱离就用 `explorer.exe "<bat>"` 拉起，下一次调用再验端口。
- **`curl -o /dev/null` 统计大文件会 exit 23**（size=0 假象），用 python urllib 读 bytes 长度验证。

## 五、发布与分发

```bash
python make_portable.py                      # dist/视频下载器-便携版.zip ≈33MB
gh repo create video-downloader --public --source . --push
gh release create v1.0.0 dist/video-downloader-portable-v1.0.0.zip --title "便携版 v1.0.0"
```

- `.gitignore` 必须挡住 `cookies.txt`、`downloads/`、`chrome-profile/`、`dist/`、`tools/*.exe` —— cookies 是用户的登录凭证。
- 上传后核对：`gh release view v1.0.0 --json assets --jq '.assets[] | "\(.name) \(.size)"'` 数字要和本地 `ls -l` 逐字节一致。
- 大二进制别进仓库，走 Release 附件。

## 六、验证清单（别只 curl 首页）

1. 解析：`/api/probe` 返回 title / heights / note
2. 下载：轮询 `/api/jobs` 到「完成」，`ffprobe` 核对时长与源一致（误差 <1s）
3. 回退：拿一个解析器不支持的站（抖音）验证自动改走嗅探
4. 扩展链路：页面里 fetch `/api/dl` 返回 200（等价于扩展的跨域调用）
5. 便携版：用它内置的 python 起服务 + 下一条视频，证明不依赖本机环境
