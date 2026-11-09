#=================================================================
# install.ps1 – Khoi tao & Cai dat Close_workflow tren Windows
#=================================================================
param(
    [switch]$NoListener = $false
)

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Dang cai dat Close_workflow cho Windows..." -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Tao cac thu muc can thiet
$dirs = @("incoming_tasks", "incoming_tasks\processed", "logs", "redis")
foreach ($d in $dirs) {
    $fullPath = Join-Path $scriptDir $d
    if (-not (Test-Path $fullPath)) {
        New-Item -ItemType Directory -Path $fullPath -Force | Out-Null
        Write-Host "Da tao thu muc: $d" -ForegroundColor Green
    }
}

# 2. Kiem tra Docker (retry/backoff: logon often starts before Docker Desktop)
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $dockerReady = $false
    for ($i = 1; $i -le 5; $i++) {
        $null = docker info 2>&1
        if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break }
        Write-Host "Docker chua san sang (lan $i/5), cho 5s..." -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
    if ($dockerReady) {
        Write-Host "Docker engine dang chay. Kich hoat Redis va Loki Stack..." -ForegroundColor Green
        docker-compose -f (Join-Path $scriptDir "docker-compose.yml") up -d redis
        docker-compose -f (Join-Path $scriptDir "docker-compose-logging.yml") up -d
    } else {
        Write-Host "Docker Desktop chua san sang sau 25s. Su dung co che Local AOF Persistence." -ForegroundColor Yellow
    }
} else {
    Write-Host "Khong tim thay Docker CLI. Su dung Local AOF Persistence." -ForegroundColor Yellow
}

# 3. Khoi dong Listener daemon
if (-not $NoListener) {
    $listenerScript = Join-Path $scriptDir "listener.py"
    # python* filter: otherwise any shell whose command text mentions
    # "listener.py" matches itself and the daemon is never started.
    $existing = Get-CimInstance Win32_Process | Where-Object { $_.Name -like "python*" -and $_.CommandLine -like "*listener.py*" }
    if ($existing) {
        Write-Host "Listener daemon da dang chay." -ForegroundColor Green
    } else {
        Write-Host "Dang khoi dong listener.py duoi nen..." -ForegroundColor Green
        $logOut = Join-Path $scriptDir "logs\listener_stdout.log"
        $logErr = Join-Path $scriptDir "logs\listener_stderr.log"
        Start-Process -FilePath "python" -ArgumentList "`"$listenerScript`"" -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr
        Start-Sleep -Seconds 1
        Write-Host "Listener da san sang tai incoming_tasks" -ForegroundColor Green
    }
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "CAI DAT HOAN TAT THANH CONG TREN WINDOWS!" -ForegroundColor Green
Write-Host "De chay task: .\run_one_task.ps1" -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan
