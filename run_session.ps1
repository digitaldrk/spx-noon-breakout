# Noon Breakout on Windows against tastytrade. Default is dry: nothing is transmitted.
#   .\run_session.ps1                         dry, broker tasty
#   .\run_session.ps1 -Mode alerts            ticket + toast, nothing transmitted
#   .\run_session.ps1 -Mode live              send to tasty_orders (sandbox unless config says production)
# Keeps Windows awake while the bot runs and restores normal sleep on exit.
# Tastytrade live auto-approves. Price caps and the 10/min limit still apply.
param(
    [ValidateSet("live", "alerts", "dry")][string]$Mode = "dry",
    [string]$Broker = "tasty"
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
# One name each. This PC launches tasty only, and tasty is the only accepted
# spelling: 'tt' and 'tastytrade' are not, and 'ib' is not a spelling of ibkr.
# ValidateSet cannot say which spelling to use, so -Broker is a plain string
# checked here, before Python starts.
$wanted = "$Broker".Trim().ToLower()
switch ($wanted) {
    "tt" {
        [Console]::Error.WriteLine("broker 'tt' is not valid; use tasty. Valid brokers: ibkr, tasty.")
        exit 2
    }
    "tastytrade" {
        [Console]::Error.WriteLine("broker 'tastytrade' is not valid; use tasty. Valid brokers: ibkr, tasty.")
        exit 2
    }
    "ib" {
        [Console]::Error.WriteLine("broker 'ib' is not valid; use ibkr. Valid brokers: ibkr, tasty.")
        exit 2
    }
}
if ($wanted -ne "tasty") {
    [Console]::Error.WriteLine("this PC runs tasty only (got '$wanted'). Use -Broker tasty.")
    exit 2
}
$Broker = $wanted
if (-not $env:SPX_NOON_HOME) { $env:SPX_NOON_HOME = $PSScriptRoot }
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
$args2 = @("-m", "spx_noon", "--mode", $Mode, "--broker", $Broker)
if (Test-Path $cfg) { $args2 += @("--config", $cfg) }
try {
    & $py @args2
} finally {
    [void][Win32.Power]::SetThreadExecutionState([uint32]"0x80000000")
}
