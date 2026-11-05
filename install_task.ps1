# install_task.ps1
# Register a Windows Task Scheduler job that runs run_daily.py every day.
# All literals in this file are ASCII on purpose: the project path contains
# non-ASCII characters and PowerShell 5.1 misreads BOM-less UTF-8 scripts.
# The project path is resolved at runtime via $PSScriptRoot, never hardcoded.
#
# Usage (run in PowerShell):
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Time "08:00" -PythonExe "C:\path\to\python.exe"
#
# Check / run once / remove:
#   Get-ScheduledTask -TaskName GitHubOpenResourceDaily
#   Start-ScheduledTask -TaskName GitHubOpenResourceDaily
#   Unregister-ScheduledTask -TaskName GitHubOpenResourceDaily -Confirm:$false
#
# NOTE: a WorkBuddy daily automation (08:00) already generates the same report.
# After enabling this task, ask WorkBuddy to pause that automation, otherwise
# both jobs run at the same time and compete for the unauthenticated GitHub
# API quota (60 requests/hour). Pick one scheduler, not both.

param(
    [string]$Time = "08:00",
    [string]$TaskName = "GitHubOpenResourceDaily",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$scriptPath = Join-Path $projectDir "run_daily.py"
if (-not (Test-Path $scriptPath)) {
    throw "run_daily.py not found: $scriptPath"
}

if ([string]::IsNullOrEmpty($PythonExe)) {
    $versionsDir = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions"
    $found = Get-ChildItem -Path $versionsDir -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) {
        $PythonExe = $found.FullName
    } else {
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($cmd) {
            $PythonExe = $cmd.Source
        } else {
            throw "python.exe not found. Pass -PythonExe with an absolute path."
        }
    }
}

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}

Write-Host "Project : configured"
Write-Host "Python  : configured"
Write-Host "Script  : run_daily.py"
Write-Host "Time    : $Time daily"

$arg = '"' + $scriptPath + '"'
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $arg -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel LeastPrivilege
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
$settings.ExecutionTimeLimit = "PT1H"

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force -Description "Daily GitHub open resource digest (Markdown + HTML)" | Out-Null

Write-Host "Registered. Current state:"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object NextRunTime | Format-Table -AutoSize
