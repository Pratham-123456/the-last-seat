# One-time local PostgreSQL bootstrap (Workflow A).
# Usage (from the project root, in PowerShell):
#     .\scripts\bootstrap_local_db.ps1
# psql will prompt for YOUR postgres superuser password. It is typed only into psql;
# it is never written to disk or into this project.
param(
    [string]$PgBin = "C:\Program Files\PostgreSQL\18\bin",
    [string]$SuperUser = "postgres",
    [string]$PgHost = "localhost",
    [int]$Port = 5432
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile (copy .env.example first)" }

# Pull the app-role password out of DATABASE_URL in .env
$line = Get-Content $envFile | Where-Object { $_ -match '^DATABASE_URL=' } | Select-Object -First 1
if ($line -notmatch '://last_seat_app:([^@]+)@') { throw "DATABASE_URL in .env must use the last_seat_app role" }
$appPassword = [System.Uri]::UnescapeDataString($Matches[1])

& "$PgBin\psql.exe" -h $PgHost -p $Port -U $SuperUser -d postgres `
    -v "app_password=$appPassword" -f (Join-Path $PSScriptRoot "bootstrap_local_db.sql")
if ($LASTEXITCODE -ne 0) { throw "bootstrap failed (psql exit $LASTEXITCODE)" }
Write-Host "Bootstrap complete." -ForegroundColor Green
