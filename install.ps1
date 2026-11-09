# Windows bootstrap: finds Python 3.9+ and hands over to install.py (all logic lives there).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
foreach ($c in @("py", "python3", "python")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
        & $c -c "import sys; sys.exit(sys.version_info < (3, 9))"
        if ($LASTEXITCODE -eq 0) { & $c install.py @args; exit $LASTEXITCODE }
    }
}
Write-Error "Python 3.9+ not found. Install from https://www.python.org/downloads/ (tick 'Add to PATH') and re-run."
