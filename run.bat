@echo off
rem Runs the bot and restarts it if it ever crashes
cd /d "%~dp0"
:loop
.venv\Scripts\python bot.py
echo Bot stopped, restarting in 10 seconds... (close this window to stop it)
timeout /t 10 /nobreak >nul
goto loop
