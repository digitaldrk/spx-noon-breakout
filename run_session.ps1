# Noon Breakout on Windows against Schwab. Default is dry: nothing is transmitted.
#   .\run_session.ps1                 dry
#   .\run_session.ps1 -Mode alerts    ticket + toast, nothing transmitted
#   .\run_session.ps1 -Mode live      send only after "yes <ticketID>" in THIS window
# Keeps Windows awake while the bot runs and restores normal sleep on exit.
param([ValidateSet("live", "alerts", "dry")][string]$Mode = "dry")
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
if (-not $env:SPX_NOON_HOME) { $env:SPX_NOON_HOME = $PSScriptRoot }
if (-not $env:SCHWAB_TOKEN_PATH) { $env:SCHWAB_TOKEN_PATH = Join-Path $env:USERPROFILE "schwab\tokens.json" }
foreach ($v in "SCHWAB_APP_KEY", "SCHWAB_APP_SECRET") {
    if (-not [Environment]::GetEnvironmentVariable($v)) { Write-Error "$v is not set (see WINDOWS_SETUP.md)" }
}
if (-not (Test-Path $env:SCHWAB_TOKEN_PATH)) { Write-Error "token file $env:SCHWAB_TOKEN_PATH not found" }
if ((Get-Date).DayOfWeek -in @("Saturday", "Sunday")) { Write-Host "weekend - not running"; exit 0 }
New-Item -ItemType Directory -Force -Path (Join-Path $env:SPX_NOON_HOME "state") | Out-Null
Add-Type -Namespace Win32 -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
'@
# ES_CONTINUOUS | ES_SYSTEM_REQUIRED (the display may still turn off)
[void][Win32.Power]::SetThreadExecutionState([uint32]"0x80000001")
$env:PYTHONUTF8 = '1'
$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$cfg = Join-Path $PSScriptRoot "config.json"
$args2 = @("-m", "spx_noon", "--mode", $Mode)
if (Test-Path $cfg) { $args2 += @("--config", $cfg) }
try {
    & $py @args2
} finally {
    [void][Win32.Power]::SetThreadExecutionState([uint32]"0x80000000")
}
