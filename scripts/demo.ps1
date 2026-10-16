# Live review demo (Workflow A). Start the API (and optionally the worker) first:
#   .\.venv\Scripts\uvicorn.exe app.main:app --port 8000
# Then, in another window:
#   powershell -ExecutionPolicy Bypass -File .\scripts\demo.ps1 [-EventId 1] [-Seat 77] [-Late]
#
# It narrates: concurrent stampede -> failed payment + retry -> delayed payment ->
# cancellation + reallocation -> idempotent replay -> (with -Late and HOLD_TTL_SECONDS=20)
# a late callback being voided. Uses seats Seat..Seat+4.
# Only additive API calls are made (no deletes); pick a seat that is still free.
param(
    [string]$Base = "http://127.0.0.1:8000",
    [int]$EventId = 1,
    [int]$Seat = 77,
    [switch]$Late
)
$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
& $py (Join-Path $PSScriptRoot "demo.py") --base $Base --event-id $EventId --seat $Seat $(if ($Late) { "--late" })
exit $LASTEXITCODE
