# PowerShell build script — Windows equivalent of build-mac.sh.
# Produces dist\LuLuBot.exe (single self-contained onefile binary)
# with the operator's bridge URL + token baked in.
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
# Only the bridge URL is baked now. The bearer token is NOT baked — each user
# pastes their own prepaid wallet token into the app at runtime.
if (-not $env:PHANTOM_BRIDGE_URL) { throw "PHANTOM_BRIDGE_URL not set" }
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
    # Token is intentionally left as the @@BRIDGE_TOKEN@@ placeholder so
    # build_config resolves it to None — users supply their own wallet token.
    Set-Content $cfgPath $content -NoNewline -Encoding UTF8

    if ((Get-Content $cfgPath -Raw) -match "@@BRIDGE_URL@@") {
        throw "BRIDGE_URL placeholder still present in $cfgPath after substitution"
    }
    Write-Host "-> build_config baked: BRIDGE_URL=$($env:PHANTOM_BRIDGE_URL) (no token baked; user enters wallet token)"

    # --- 3. PyInstaller ---
    if (Test-Path build) { Remove-Item -Recurse -Force build }
    if (Test-Path dist) { Remove-Item -Recurse -Force dist }

    Write-Host "-> running PyInstaller (this takes 1-3 min on first build) ..."
    python -m PyInstaller lulubot.spec --noconfirm --clean

    # Onefile build: single self-contained .exe in dist/.
    $exePath = "dist\LuLuBot.exe"
    if (-not (Test-Path $exePath)) {
        throw "PyInstaller didn't produce $exePath"
    }

    $sizeExe = "{0:N0} MB" -f ((Get-Item $exePath).Length / 1MB)

    Write-Host ""
    Write-Host "============================================================="
    Write-Host "  build OK"
    Write-Host "============================================================="
    Write-Host "  artifact : $exePath ($sizeExe)"
    Write-Host "  baked URL: $($env:PHANTOM_BRIDGE_URL)"
    Write-Host ""
    Write-Host "  User flow:"
    Write-Host "    1. Hand over $exePath"
    Write-Host "    2. First open: SmartScreen says 'Windows protected your"
    Write-Host "       PC' -> More info -> Run anyway"
    Write-Host "    3. Type task, press 运行"
    Write-Host "============================================================="
} finally {
    # Always restore build_config so the secret doesn't end up committed.
    Copy-Item $backup $cfgPath -Force
    Remove-Item $backup -ErrorAction SilentlyContinue
    Write-Host "-> restored $cfgPath"
}
