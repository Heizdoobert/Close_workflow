#=================================================================
# run_one_task.ps1 – Thuc thi mot task trong 1 session tren Windows
#=================================================================
param(
    [Parameter(Mandatory=$false, Position=0)]
    [string]$TaskMessage = "toi co task cho ban:[Xay API quan ly san pham]",
    [Parameter(Mandatory=$false, Position=1)]
    [string]$Model = "gpt-4o-mini"
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cliPath = Join-Path $scriptDir "close_workflow_cli.py"

# PS5.1 decodes native stdout with the OEM codepage (cp437) and mangles UTF-8 JSON
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Dang thuc thi task (Model: $Model)..." -ForegroundColor Cyan
Write-Host "Task: $TaskMessage" -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan

$env:AGY_MODEL = $Model
$jsonOutput = python "$cliPath" "$TaskMessage" "$Model"

try {
    $parsed = $jsonOutput | ConvertFrom-Json
    Write-Host "`nKET QUA (DATA):" -ForegroundColor Green
    $parsed.data | Format-List

    Write-Host "`nLICH SU THUC THI (HISTORY LOG - 12 BUOC DAG):" -ForegroundColor Green
    foreach ($entry in $parsed.history) {
        $act = $entry.action
        $msg = if ($entry.message) { $entry.message } else { $entry.key }
        Write-Host "  [$act] $msg" -ForegroundColor Gray
    }

    $resFile = Join-Path $scriptDir "last_session_result.json"
    # Out-File -Encoding utf8 adds a BOM that breaks strict json.load readers
    [System.IO.File]::WriteAllText($resFile, $jsonOutput, [System.Text.UTF8Encoding]::new($false))
    Write-Host "`nKet qua JSON da luu tai: $resFile" -ForegroundColor Cyan
} catch {
    Write-Host "`nRaw Output:" -ForegroundColor White
    $jsonOutput
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "TASK HOAN THANH!" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan
