@echo off
chcp 936 >nul
title 安装视频下载器扩展
echo.
echo   正在打开 Chrome 扩展页和扩展文件夹...
echo.
start "" chrome.exe "chrome://extensions/"
explorer.exe "%~dp0extension"
echo   安装三步：
echo     1. 在扩展页右上角打开「开发者模式」
echo     2. 点左上角「加载已解压的扩展程序」
echo     3. 选中刚弹出的 extension 文件夹
echo.
echo   装好后把「视频下载器」固定到工具栏，刷到想下的视频点一下就行。
echo.
pause
