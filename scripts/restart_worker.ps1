# ============================================================================
# restart_worker.ps1 - Update and restart the Penny Windows worker
# ============================================================================
# Pulls the latest code, stops the running worker, waits for the scheduled
# task to finish (starting it while the old run is still "Running" is silently
# ignored by Task Scheduler), starts it again and checks /health.
#
#   .\scripts\restart_worker.ps1            # pull + restart
#   .\scripts\restart_worker.ps1 -NoPull    # restart only
#
# If scripts are blocked: powershell -ExecutionPolicy Bypass -File .\scripts\restart_worker.ps1
# ============================================================================
param(
    [switch]$NoPull,
    [string]$TaskName = "PennyWindowsCADWorker",
    [int]$Port = 8085
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

if (-not $NoPull) {
    Write-Host "Pulling latest code..."
    git pull origin main
    if ($LASTEXITCODE -ne 0) { Write-Host "git pull failed" -ForegroundColor Red; exit 1 }
}

Write-Host "Stopping worker on port $Port..."
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

$deadline = (Get-Date).AddSeconds(30)
while ((Get-ScheduledTask $TaskName).State -eq "Running" -and (Get-Date) -lt $deadline) {
    Start-Sleep 1
}

Write-Host "Starting task $TaskName..."
Start-ScheduledTask -TaskName $TaskName

$deadline = (Get-Date).AddSeconds(60)
$health = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep 3
    try {
        $health = Invoke-RestMethod "http://localhost:$Port/health" -TimeoutSec 5
        break
    } catch { }
}

if ($null -eq $health) {
    Write-Host "Worker did not come up within 60 s." -ForegroundColor Red
    Write-Host "Run scripts\start_worker.bat from the Remote Desktop session to see the error."
    exit 1
}

$health | Select-Object commit_running, restart_needed, work_hours_now, sap2000_held_open, autocad_held_open, dwg_engine | Format-List
if ($health.restart_needed) {
    Write-Host "Running commit differs from the checkout - restart again." -ForegroundColor Yellow
    exit 1
}
Write-Host "Worker is up." -ForegroundColor Green
