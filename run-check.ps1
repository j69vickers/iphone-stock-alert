# Runs one stock check. Scheduled every 5 minutes by Task Scheduler (task: "iPhone Stock Alert").
# Topic is read from ntfy_topic.txt (git-ignored) so it never goes into the repo.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:NTFY_TOPIC = (Get-Content "$PSScriptRoot\ntfy_topic.txt" -Raw).Trim()
$python = "C:\Users\josh\AppData\Local\Python\pythoncore-3.15-64\python.exe"
$log = "$PSScriptRoot\stock-check.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 500KB)) { Move-Item $log "$log.old" -Force }
"=== $(Get-Date -Format s)" | Add-Content $log -Encoding utf8
& $python "$PSScriptRoot\check.py" 2>&1 | ForEach-Object { "$_" } | Add-Content $log -Encoding utf8
