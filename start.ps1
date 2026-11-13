# Starts seen: a Cloudflare quick tunnel (public https URL) + the server.
# Usage:  .\start.ps1            (Ctrl+C to stop both)
#         .\start.ps1 -Mock      (fake results, no API calls)
param([switch]$Mock, [int]$Port = 8000)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) {
    foreach ($p in "$PSScriptRoot\tools\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe", "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
                   "$env:LOCALAPPDATA\Microsoft\WinGet\Links\cloudflared.exe") {
        if (Test-Path $p) { $cf = $p; break }
    }
}
if (-not $cf) { throw "cloudflared not found. Install it with: winget install Cloudflare.cloudflared" }

$log = Join-Path $env:TEMP "seen-tunnel.log"
Remove-Item $log -ErrorAction SilentlyContinue
$tunnel = Start-Process -FilePath $cf -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://localhost:$Port" `
    -RedirectStandardError $log -WindowStyle Hidden -PassThru

Write-Host "Starting tunnel..." -NoNewline
$url = $null
for ($i = 0; $i -lt 60 -and -not $url; $i++) {
    Start-Sleep -Milliseconds 500
    Write-Host "." -NoNewline
    if (Test-Path $log) {
        $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
        if ($m) { $url = $m.Matches[0].Value }
    }
}
Write-Host ""
if (-not $url) { Stop-Process -Id $tunnel.Id -Force; throw "Tunnel didn't start. See $log" }

$env:PUBLIC_BASE_URL = $url
$env:PYTHONIOENCODING = "utf-8"
if ($Mock) { $env:MOCK = "1" } else { Remove-Item Env:MOCK -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host "  Public URL:     $url" -ForegroundColor Green
Write-Host "  Shortcut URL:   $url/api/analyze" -ForegroundColor Green
Write-Host "  Test on phone:  $url/?token=<your APP_TOKEN from .env>"
Write-Host "  (The URL changes each time you run this; update the Shortcut if it does.)"
Write-Host ""

try {
    & .\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port $Port
} finally {
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
}
