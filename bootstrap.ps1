# Creates .venv and installs mapgen in editable mode with dev extras.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path "$root\.venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv "$root\.venv"
}

$python = "$root\.venv\Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -e ".[dev]"

Write-Host ""
Write-Host "Done. Activate with:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "Then run:"
Write-Host "  mapgen ui"
