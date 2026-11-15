# Installs / updates "My Library" on Windows. Run in PowerShell:
#   irm https://raw.githubusercontent.com/telegram-video12/video-/HEAD/install-windows.ps1 | iex
# Running it again = update (your data folder is kept).
$ErrorActionPreference = "Stop"
$Dir    = Join-Path $env:USERPROFILE "tgvideo"
$Branch = "ccr-6961d79f-x3exxm"
$Zip    = "https://github.com/telegram-video12/video-/archive/refs/heads/$Branch.zip"

function Say($t) { Write-Host "`n==> $t" -ForegroundColor Yellow }

# 1. Python
# Note: Windows ships a fake "python" (Microsoft Store shortcut) that only prints an error,
# so we look for a real installation and use its full path everywhere.
function Find-Python {
    $cands = @()
    $cands += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue
    $cands += Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $cands += Get-Item $cmd.Source }
    foreach ($c in ($cands | Sort-Object FullName -Descending)) {
        $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        $v = & $c.FullName --version 2>&1 | Out-String
        $ErrorActionPreference = $old
        if ($v -match "Python 3\.(1[0-9])") { return $c.FullName }
    }
    return $null
}

Say "Step 1/4: Python"
$Python = Find-Python
if (-not $Python) {
    Write-Host "Installing Python 3.12 (a window may ask for permission - click Yes)..."
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $ErrorActionPreference = $old
    $Python = Find-Python
}
if (-not $Python) {
    Write-Host "`nPython was not found. Install it from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "(tick 'Add python.exe to PATH'), then run this command again." -ForegroundColor Red
    return
}
Write-Host "Using Python: $Python"

# 2. Download the site (data folder is never touched)
Say "Step 2/4: Downloading the site"
$tmp = Join-Path $env:TEMP "tgvideo-download"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Path $tmp | Out-Null
Invoke-WebRequest -Uri $Zip -OutFile (Join-Path $tmp "site.zip") -UseBasicParsing
Expand-Archive -Path (Join-Path $tmp "site.zip") -DestinationPath $tmp -Force
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
New-Item -ItemType Directory -Path $Dir -Force | Out-Null
Copy-Item -Path (Join-Path $src.FullName "*") -Destination $Dir -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $Dir "data") -Force | Out-Null
Remove-Item $tmp -Recurse -Force

# 3. Components
Say "Step 3/4: Installing components"
$old = $ErrorActionPreference; $ErrorActionPreference = "Continue"   # pip prints notices on stderr
& $Python -m pip install --upgrade pip --quiet 2>&1 | Out-Null
& $Python -m pip install -r (Join-Path $Dir "requirements.txt") --quiet
$ErrorActionPreference = $old
if ($LASTEXITCODE -ne 0) { Write-Host "Installing components failed (see above)." -ForegroundColor Red; return }

# 4. Desktop shortcuts
Say "Step 4/4: Desktop shortcuts"
$start = Join-Path $Dir "start-windows.bat"
Set-Content -Path $start -Encoding ASCII -Value @"
@echo off
title My Library
cd /d "%~dp0"
set HOST=0.0.0.0
"$Python" -m app
pause
"@
$update = Join-Path $Dir "update-windows.bat"
Set-Content -Path $update -Encoding ASCII -Value @"
@echo off
title Update My Library
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/telegram-video12/video-/HEAD/install-windows.ps1 | iex"
pause
"@
$desk = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
foreach ($s in @(@("My Library", $start), @("Update My Library", $update))) {
    $lnk = $shell.CreateShortcut((Join-Path $desk ($s[0] + ".lnk")))
    $lnk.TargetPath = $s[1]
    $lnk.WorkingDirectory = $Dir
    $lnk.Save()
}

Write-Host "`n*** INSTALLATION COMPLETE ***" -ForegroundColor Green
Write-Host "Site folder: $Dir"
Write-Host "Your data folder (put library.db and config.json here): $(Join-Path $Dir 'data')"
Write-Host "To start: double-click 'My Library' on the desktop."
