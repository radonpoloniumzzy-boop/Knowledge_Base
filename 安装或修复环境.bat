@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0\00_Scripts\setup_knowledge_forge.ps1"
echo.
pause
