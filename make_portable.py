# -*- coding: utf-8 -*-
"""打一个「换台电脑双击就能跑」的便携版：内置 Python + yt-dlp。

用法：python make_portable.py
产物：dist/视频下载器-便携版/  和  dist/视频下载器-便携版.zip
"""
import os
import shutil
import sys
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(BASE, "dist")
PKG_NAME = "视频下载器-便携版"
PKG = os.path.join(DIST, PKG_NAME)
PROXY = os.environ.get("VD_PROXY") or "http://127.0.0.1:7897"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"

PY_VER = "3.11.9"
PY_ZIP = f"python-{PY_VER}-embed-amd64.zip"
PY_URL = f"https://www.python.org/ftp/python/{PY_VER}/{PY_ZIP}"
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"

COPY_FILES = ["app.py", "启动下载器.bat", "安装扩展.bat", "使用说明.txt", "README.md"]
COPY_DIRS = ["extension"]


def fetch(url, dest):
    last = None
    for proxy in (None, PROXY):
        try:
            handlers = []
            if proxy:
                handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with opener.open(req, timeout=300) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 512 * 1024)
            if os.path.getsize(dest) > 1024:
                print(f"    下载完成 {os.path.getsize(dest)/1048576:.1f} MB")
                return dest
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"下载失败 {url}: {last}")


def main():
    os.makedirs(DIST, exist_ok=True)
    if os.path.exists(PKG):
        shutil.rmtree(PKG)
    os.makedirs(PKG)
    os.makedirs(os.path.join(PKG, "tools"), exist_ok=True)
    os.makedirs(os.path.join(PKG, "downloads"), exist_ok=True)

    # 1) 内置 Python
    pyz = os.path.join(DIST, PY_ZIP)
    if not os.path.exists(pyz):
        print("[1/4] 下载 Python 嵌入式包 …")
        fetch(PY_URL, pyz)
    else:
        print("[1/4] 复用已下载的", PY_ZIP)
    pydir = os.path.join(PKG, "python")
    os.makedirs(pydir, exist_ok=True)
    with zipfile.ZipFile(pyz) as z:
        z.extractall(pydir)
    # 让 python 能 import 同目录模块
    for f in os.listdir(pydir):
        if f.endswith("._pth"):
            p = os.path.join(pydir, f)
            with open(p, "r", encoding="utf-8") as fh:
                lines = [l.rstrip("\n") for l in fh]
            if "import site" not in lines:
                lines.append("import site")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
    print("    Python 就绪:", os.path.join(pydir, "python.exe"))

    # 2) 内置 yt-dlp
    print("[2/4] 取 yt-dlp.exe …")
    ytdlp = os.path.join(PKG, "tools", "yt-dlp.exe")
    fetch(YTDLP_URL, ytdlp)

    # 3) 拷程序
    print("[3/4] 拷贝程序文件 …")
    for f in COPY_FILES:
        src = os.path.join(BASE, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(PKG, f))
        else:
            print("    [跳过] 缺少", f)
    for d in COPY_DIRS:
        src = os.path.join(BASE, d)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(PKG, d), ignore=shutil.ignore_patterns("*.log", "*.tmp"))

    # 4) 打包
    print("[4/4] 打 zip …")
    zpath = os.path.join(DIST, PKG_NAME + ".zip")
    if os.path.exists(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _, files in os.walk(PKG):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, DIST)
                z.write(full, rel)
    print("完成:", zpath, f"{os.path.getsize(zpath)/1048576:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
