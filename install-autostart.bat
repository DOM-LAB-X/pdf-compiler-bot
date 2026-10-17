@echo off
rem Starts the bot in the background whenever the VM boots (no login needed). Run as Administrator.
net session >nul 2>nul || (echo Right-click this file and choose "Run as administrator". & pause & exit /b 1)
schtasks /create /tn "PdfCompilerBot" /tr "\"%~dp0run.bat\"" /sc onstart /ru SYSTEM /rl highest /f
schtasks /run /tn "PdfCompilerBot"
echo.
echo Installed and started. It will now start every time Windows boots.
echo To remove it: schtasks /delete /tn "PdfCompilerBot" /f
pause
