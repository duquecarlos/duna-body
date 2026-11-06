# One-time setup on Carlos's PC: virtual environment + dependencies.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -q -r requirements.txt
Write-Host "Ready. Next: .\.venv\Scripts\python -m body.login"
