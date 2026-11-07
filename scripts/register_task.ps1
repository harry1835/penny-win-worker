# ============================================================================
# register_task.ps1 - Register Penny Windows Worker in Windows Task Scheduler
# ============================================================================
# IMPORTANT: Both AutoCAD COM and SAP2000 OAPI require an interactive user
# session (LogonType Interactive) rather than a headless Session 0 Windows
# Service, otherwise COM CoCreateInstance fails with CO_E_SERVER_EXEC_FAILURE.
# ============================================================================

$ErrorActionPreference = "Stop"
$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BatPath = Join-Path $RepoDir "scripts\start_worker.bat"
$TaskName = "PennyWindowsCADWorker"

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatPath`"" -WorkingDirectory $RepoDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force
Write-Host "Registered Scheduled Task '$TaskName' to run in interactive session for user '$env:USERNAME'."
Write-Host "To start immediately: Start-ScheduledTask -TaskName '$TaskName'"
