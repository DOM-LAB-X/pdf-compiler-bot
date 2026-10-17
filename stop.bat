@echo off
rem Stops the background bot started by install-autostart.bat (run as Administrator)
schtasks /end /tn "PdfCompilerBot"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object CommandLine -like '*bot.py*' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Stopped. Run install-autostart.bat (or reboot) to start it again.
pause
