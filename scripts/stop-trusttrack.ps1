$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$RunDir = Join-Path $Root ".run"
$PidPath = Join-Path $RunDir "trusttrack.pid"

function Get-DescendantProcessIds {
    param([int]$ParentId)

    $Children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ParentId" -ErrorAction SilentlyContinue)
    foreach ($Child in $Children) {
        [int]$Child.ProcessId
        Get-DescendantProcessIds -ParentId ([int]$Child.ProcessId)
    }
}

if (-not (Test-Path -LiteralPath $PidPath)) {
    Write-Host "TrustTrack is not running."
    exit 0
}

$PidValue = [int](Get-Content -LiteralPath $PidPath -TotalCount 1)
$RootProcess = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
if (-not $RootProcess) {
    Remove-Item -LiteralPath $PidPath -Force
    Write-Host "TrustTrack was not running. Removed stale PID file."
    exit 0
}

$ProcessIds = @((Get-DescendantProcessIds -ParentId $PidValue) + $PidValue) | Select-Object -Unique
foreach ($ProcessId in ($ProcessIds | Sort-Object -Descending)) {
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $PidPath -Force
Write-Host "TrustTrack stopped."
