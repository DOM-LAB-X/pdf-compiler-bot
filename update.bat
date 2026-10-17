@echo off
rem Updates the bot to the newest GitHub release. Run as administrator if you use auto-start.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
pause
