# Read-only end-of-day note. Does not place orders.
Set-Location $PSScriptRoot
if (-not $env:SPX_NOON_HOME) { $env:SPX_NOON_HOME = $PSScriptRoot }
$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
& $py -m spx_noon wrap @args
exit $LASTEXITCODE
