# run_task.ps1 – PowerShell wrapper
param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$TaskMessage,
    [Parameter(Mandatory=$false, Position=1)]
    [string]$Model = "gpt-4o-mini"
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cliPath = Join-Path $scriptDir "close_workflow_cli.py"
# PS5.1 decodes native stdout with the OEM codepage (cp437) and mangles UTF-8 JSON
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
python "$cliPath" "$TaskMessage" "$Model"
