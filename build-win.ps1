# PowerShell build script — Windows equivalent of build-mac.sh.
# Produces dist\噜噜机器人-win.zip with the operator's bridge URL + token
# baked in.
#
# Required env (set by Actions secrets in CI, or `$env:` in PS):
#   PHANTOM_BRIDGE_URL    — public URL of the operator's bridge
#   PHANTOM_BRIDGE_TOKEN  — shared bearer token CN clients use
#
# Usage:
#   $env:PHANTOM_BRIDGE_URL = "https://bridge.z1nexusn1.org"
#   $env:PHANTOM_BRIDGE_TOKEN = "pc_..."
#   .\build-win.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- 1. Sanity checks ---
if (-not $env:PHANTOM_BRIDGE_URL) { throw "PHANTOM_BRIDGE_URL not set" }
if (-not $env:PHANTOM_BRIDGE_TOKEN) { throw "PHANTOM_BRIDGE_TOKEN not set" }
if ($env:PHANTOM_BRIDGE_URL -notmatch '^(https://|http://127\.0\.0\.1)') {
    throw "PHANTOM_BRIDGE_URL must be https:// (or http://127.0.0.1 for local test)"
}

# Confirm PyInstaller available.
python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "-> installing PyInstaller ..."
    python -m pip install -q pyinstaller
}

# Confirm app deps importable. If you forgot to install them, fail with a
# pointer rather than letting PyInstaller produce a half-broken bundle.
python -c "import PySide6, PIL, pyautogui, mss" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "app deps missing — run: python -m pip install -r requirements-app.txt"
}

# --- 2. Inject baked config ---
$cfgPath = "app\build_config.py"
$backup = [System.IO.Path]::GetTempFileName()
Copy-Item $cfgPath $backup

try {
    $content = Get-Content $cfgPath -Raw
    $content = $content.Replace("@@BRIDGE_URL@@", $env:PHANTOM_BRIDGE_URL)
    $content = $content.Replace("@@BRIDGE_TOKEN@@", $env:PHANTOM_BRIDGE_TOKEN)
    Set-Content $cfgPath $content -NoNewline -Encoding UTF8

    if ((Get-Content $cfgPath -Raw) -match "@@BRIDGE_URL@@|@@BRIDGE_TOKEN@@") {
        throw "placeholders still present in $cfgPath after substitution"
    }
    Write-Host "-> build_config baked: BRIDGE_URL=$($env:PHANTOM_BRIDGE_URL) (token hidden)"

    # --- 3. PyInstaller ---
    if (Test-Path build) { Remove-Item -Recurse -Force build }
    if (Test-Path dist) { Remove-Item -Recurse -Force dist }

    Write-Host "-> running PyInstaller (this takes 1-3 min on first build) ..."
    python -m PyInstaller phantom-click.spec --noconfirm --clean

    $appDir = "dist\噜噜机器人"
    $exePath = "$appDir\phantom-click.exe"
    if (-not (Test-Path $exePath)) {
        throw "PyInstaller didn't produce $exePath"
    }

    # --- 4. Zip for distribution ---
    $zipPath = "dist\噜噜机器人-win.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    # Compress-Archive handles unicode names; the resulting zip extracts cleanly
    # on macOS/Linux/Windows alike.
    Compress-Archive -Path $appDir -DestinationPath $zipPath -CompressionLevel Optimal

    $sizeBundle = "{0:N0} MB" -f ((Get-ChildItem $appDir -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB)
    $sizeZip = "{0:N0} MB" -f ((Get-Item $zipPath).Length / 1MB)

    Write-Host ""
    Write-Host "============================================================="
    Write-Host "  build OK"
    Write-Host "============================================================="
    Write-Host "  bundle      : $appDir ($sizeBundle)"
    Write-Host "  distribution: $zipPath ($sizeZip)"
    Write-Host "  baked URL   : $($env:PHANTOM_BRIDGE_URL)"
    Write-Host ""
    Write-Host "  CN user flow:"
    Write-Host "    1. Download $zipPath"
    Write-Host "    2. Unzip somewhere (e.g. Desktop)"
    Write-Host "    3. First open of phantom-click.exe: SmartScreen says"
    Write-Host "       'Windows protected your PC' -> More info -> Run anyway"
    Write-Host "    4. Type task, press 运行"
    Write-Host "============================================================="
} finally {
    # Always restore build_config so the secret doesn't end up committed.
    Copy-Item $backup $cfgPath -Force
    Remove-Item $backup -ErrorAction SilentlyContinue
    Write-Host "-> restored $cfgPath"
}
