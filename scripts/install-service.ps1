# Install unified-brain as a Windows scheduled task.
# Runs on user logon, restarts on failure.

$ScriptDir = Split-Path -Parent $PSCommandPath
$RunBat = Join-Path $ScriptDir "run.bat"
$ProjectDir = Split-Path -Parent $ScriptDir
$TaskName = "unified-brain"

$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$RunBat`"" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Unified Brain - multi-channel event analysis service" `
    -Force

Write-Host "Scheduled task '$TaskName' installed."
Write-Host "Start:  schtasks /run /tn $TaskName"
Write-Host "Stop:   schtasks /end /tn $TaskName"
Write-Host "Remove: Unregister-ScheduledTask -TaskName $TaskName"
