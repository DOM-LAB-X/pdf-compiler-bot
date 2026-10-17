@echo off
rem One-time setup: installs Python packages into .venv and creates .env
cd /d "%~dp0"

where py >nul 2>nul || (
  echo Python not found. Install it with:  winget install Python.Python.3.13
  echo then close this window and run setup.bat again.
  pause & exit /b 1
)

if not exist .venv py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt || (pause & exit /b 1)

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo Created .env - opening it now. Paste your DISCORD_TOKEN, save, and close Notepad.
  notepad .env
)

if not exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
  echo.
  echo Optional: for "sort by date" to read Timemark stamps, install Tesseract:
  echo   winget install UB-Mannheim.TesseractOCR
)

echo.
echo Done. Run run.bat to start the bot, or install-autostart.bat to start it with Windows.
pause
