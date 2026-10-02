@echo off
rem SRHQ 超分 DNG 解码器启动器
rem 用法1：把 .dng 文件拖到本文件上 → 命令行直解
rem 用法2：双击本文件 → 打开图形界面
set PYTHONPATH=%~dp0libs314
if "%~1"=="" (
    start "" "C:\Python314\pythonw.exe" "%~dp0srhq_decoder.py"
) else (
    "C:\Python314\python.exe" "%~dp0srhq_decoder.py" %*
    pause
)
