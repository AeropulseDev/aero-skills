@echo off
rem loop-watch setup: token, Task Scheduler, test (see setup.ps1)
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
