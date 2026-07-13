@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0\00_Scripts\start_knowledge_forge.ps1"
echo.
echo Knowledge Forge is ready. You can close this window; the local service will keep running.
pause
