param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$RunDir = Join-Path $Root ".run"
$PidPath = Join-Path $RunDir "trusttrack.pid"
$OutPath = Join-Path $RunDir "trusttrack.out.log"
$ErrPath = Join-Path $RunDir "trusttrack.err.log"

New-Item -ItemType Directory -Path $RunDir -Force | Out-Null

if (Test-Path -LiteralPath $PidPath) {
    $ExistingPid = [int](Get-Content -LiteralPath $PidPath -TotalCount 1)
    $ExistingProcess = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
    if ($ExistingProcess) {
        Write-Host "TrustTrack is already running with PID $ExistingPid."
        Write-Host "Site URL: http://${HostName}:$Port/"
        exit 0
    }
    Remove-Item -LiteralPath $PidPath -Force
}

$Arguments = @(
    "run",
    "python",
    "manage.py",
    "run_trusttrack",
    "--host",
    $HostName,
    "--port",
    "$Port"
)

$Process = Start-Process `
    -FilePath "uv" `
    -ArgumentList $Arguments `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutPath `
    -RedirectStandardError $ErrPath `
    -PassThru

Set-Content -LiteralPath $PidPath -Value $Process.Id

Write-Host "TrustTrack started with PID $($Process.Id)."
Write-Host "Site URL: http://${HostName}:$Port/"
Write-Host "Logs: $OutPath"
Write-Host "Errors: $ErrPath"
