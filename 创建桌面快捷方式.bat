@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0\00_Scripts\create_desktop_shortcut.ps1"
echo.
pause
