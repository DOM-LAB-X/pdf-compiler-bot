# Downloads the latest release from GitHub and installs it over this folder.
# Your .env (bot token) and .venv are never touched.
$ErrorActionPreference = 'Stop'
$dir  = $PSScriptRoot
$repo = 'DOM-LAB-X/pdf-compiler-bot'
$task = 'PdfCompilerBot'

$current = if (Test-Path "$dir\VERSION") { (Get-Content "$dir\VERSION").Trim() } else { 'unknown' }
$rel     = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'pdf-compiler-bot' }
$latest  = $rel.tag_name.TrimStart('v')
if ($current -eq $latest) { Write-Host "Already up to date (v$current)."; exit 0 }
Write-Host "Updating v$current -> v$latest"

$asset = $rel.assets | Where-Object name -like '*.zip' | Select-Object -First 1
$tmp   = Join-Path $env:TEMP "pdf-compiler-bot-$latest"
Remove-Item $tmp, "$tmp.zip" -Recurse -Force -ErrorAction SilentlyContinue
Invoke-WebRequest $asset.browser_download_url -OutFile "$tmp.zip" -UseBasicParsing
Expand-Archive "$tmp.zip" $tmp -Force

# Stop the bot if it's running
cmd /c "schtasks /end /tn $task >nul 2>&1"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object CommandLine -like '*bot.py*' |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Copy-Item "$tmp\pdf-compiler-bot\*" $dir -Recurse -Force
& "$dir\.venv\Scripts\pip" install -q -r "$dir\requirements.txt"
Remove-Item $tmp, "$tmp.zip" -Recurse -Force -ErrorAction SilentlyContinue

# Restart it if auto-start is installed
cmd /c "schtasks /run /tn $task >nul 2>&1"
if ($LASTEXITCODE -eq 0) { Write-Host "Updated to v$latest and restarted." }
else { Write-Host "Updated to v$latest. Start it again with run.bat." }
