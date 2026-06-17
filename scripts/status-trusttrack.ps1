param(
    [int]$Tail = 0
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$RunDir = Join-Path $Root ".run"
$PidPath = Join-Path $RunDir "trusttrack.pid"
$OutPath = Join-Path $RunDir "trusttrack.out.log"
$ErrPath = Join-Path $RunDir "trusttrack.err.log"

if (-not (Test-Path -LiteralPath $PidPath)) {
    Write-Host "TrustTrack is not running."
    exit 0
}

$PidValue = [int](Get-Content -LiteralPath $PidPath -TotalCount 1)
$Process = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
if (-not $Process) {
    Write-Host "TrustTrack is not running, but a stale PID file exists."
    exit 1
}

Write-Host "TrustTrack is running with PID $PidValue."
Write-Host "Logs: $OutPath"
Write-Host "Errors: $ErrPath"

if ($Tail -gt 0) {
    if (Test-Path -LiteralPath $OutPath) {
        Write-Host ""
        Write-Host "Last $Tail output log lines:"
        Get-Content -LiteralPath $OutPath -Tail $Tail
    }
    if (Test-Path -LiteralPath $ErrPath) {
        Write-Host ""
        Write-Host "Last $Tail error log lines:"
        Get-Content -LiteralPath $ErrPath -Tail $Tail
    }
}
