# setup.ps1 — one-time setup on any Windows machine
# Run: .\setup.ps1

$ErrorActionPreference = "Stop"

Write-Host "`n  Swing Trading Workstation — Setup`n" -ForegroundColor Cyan

# 1. Python check
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "  ✗ Python not found. Install Python 3.11+ from https://python.org" -ForegroundColor Red
    exit 1
}
$ver = python --version 2>&1
Write-Host "  ✓ $ver" -ForegroundColor Green

# 2. Virtual environment
if (-not (Test-Path ".venv")) {
    Write-Host "  Creating virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
}
Write-Host "  ✓ .venv ready" -ForegroundColor Green

# 3. Install dependencies
Write-Host "  Installing dependencies (this takes ~60 seconds)..." -ForegroundColor Yellow
.\.venv\Scripts\pip install --quiet --upgrade pip
.\.venv\Scripts\pip install --quiet -r requirements.txt
Write-Host "  ✓ Dependencies installed" -ForegroundColor Green

# 4. Create journal folder if missing
if (-not (Test-Path "journal")) {
    New-Item -ItemType Directory -Path "journal" | Out-Null
    Write-Host "  ✓ journal/ created" -ForegroundColor Green
}

# 5. Verify
Write-Host "`n  Verifying installation..." -ForegroundColor Yellow
$result = .\.venv\Scripts\python run.py --help 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ Setup complete`n" -ForegroundColor Green
} else {
    Write-Host "  ✗ Something went wrong. Run: .\.venv\Scripts\python run.py --help" -ForegroundColor Red
    exit 1
}

Write-Host "  Next steps:" -ForegroundColor Cyan
Write-Host "    1. Edit config\config.py → set account_capital to your Zerodha balance"
Write-Host "    2. Run: .\.venv\Scripts\python run.py --kite-login"
Write-Host "    3. Run: .\.venv\Scripts\python run.py --install-scheduler"
Write-Host "    4. Run: .\.venv\Scripts\python run.py --today`n"
