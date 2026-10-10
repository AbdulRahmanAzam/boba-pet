# Boba Pet one-line installer for Windows.
#
#   irm https://raw.githubusercontent.com/AbdulRahmanAzam/boba-pet/main/install.ps1 | iex
#
# Installs uv (if missing), a private Python 3.12 with Tk, and Boba Pet as an
# isolated tool; downloads the offline voice and search models; adds a Start
# menu shortcut; then starts the pet. Nothing needs admin rights. Set
# $env:BOBA_PET_AUTOSTART = "1" first to also start the pet with Windows.

$ErrorActionPreference = "Stop"
$Source = "boba-pet"  # from PyPI

function Step($text) { Write-Host "==> $text" -ForegroundColor Magenta }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Step "Installing uv (Python package manager)"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Step "Installing Boba Pet"
uv tool install --force --python 3.12 $Source
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed" }
uv tool update-shell | Out-Null

$Bin = (uv tool dir --bin).Trim()
Step "Downloading offline models (about 200 MB, one time)"
if ($env:BOBA_PET_AUTOSTART -eq "1") { & "$Bin\boba-pet-setup.exe" --autostart } else { & "$Bin\boba-pet-setup.exe" }

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "Tip: Boba works best with Claude Code. Install it with:" -ForegroundColor Yellow
    Write-Host "  irm https://claude.ai/install.ps1 | iex" -ForegroundColor Yellow
    Write-Host "Or pick Codex, Gemini or Groq from the pet's right-click menu > AI provider." -ForegroundColor Yellow
}

Step "Starting Boba Pet"
Start-Process "$Bin\boba-pet.exe"
Write-Host "Done. Boba is on your desktop. Right-click it for the menu." -ForegroundColor Green
