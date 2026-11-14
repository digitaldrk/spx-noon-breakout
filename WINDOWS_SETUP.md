# Windows setup (tastytrade)

This is the Windows machine. A Mac runs IBKR paper through `run.sh` and `deploy/install_launchd.sh`, using `config.example.json`. On this PC, copy `config.windows.example.json` to `config.json`.

`auto` on Windows is tastytrade. Quotes, 15-minute bars, 1-minute bars, the SPXW chain, greeks, and orders all come from tastytrade. Tastytrade is the only broker this PC runs.

## What it does

Noon Breakout, plan v2 version B: one 0DTE SPX debit vertical on the first qualifying 15-minute close from noon to 1:00 PM ET. Long strike nearest 75 delta. Take-profit at +30% of the debit. Flat two hours after the entry bar. The session does not chase.

| Command | Orders |
| --- | --- |
| `.\run_session.ps1` | Dry. Nothing is sent. |
| `.\run_session.ps1 -Mode alerts` | Nothing is sent. The ticket is printed and toasted. |
| `.\run_session.ps1 -Mode live` | Sends. Tastytrade auto-approves. There is no prompt. The price cap is the mid limit, and at most 10 place/replace/cancel requests go out per minute. |

Where live orders go is `tasty_orders` in `config.json`, not an environment variable:

| `tasty_orders` | Host | Money |
| --- | --- | --- |
| `sandbox` (default) | `api.cert.tastyworks.com` | Simulated. Only sandbox credentials are loaded for orders. |
| `production` | `api.tastyworks.com` | Real money. Requires `tasty_data=production`. |

`tasty_data` defaults to `production`. Market data does not fall back to the sandbox. Startup prints `DATA=<host> ORDERS=<host> REAL MONEY=YES/NO`. The production data session is never passed to an order call.

## Config file

```powershell
copy config.windows.example.json config.json
```

`config.json` stays on this PC. The example in git has no secrets.

| Key | Example | Role |
| --- | --- | --- |
| `mode` | `dry` | `dry` and `alerts` never send. `live` auto-approves and sends. |
| `tasty_data` | `production` | Quote, bar, chain, and greek host. |
| `tasty_orders` | `sandbox` | Order host. `production` is real money. |
| `account` | `""` | Blank uses the only account on the order session. Set it when there is more than one. |
| `base_dir` | `C:/spx-noon-breakout` | State, logs, and reports. Order ids are `state\tt_orders-<date>.json`. |

## One-time install

1. Install Python 3.11 or newer and tick "Add python.exe to PATH".
2. Unzip to `C:\spx-noon-breakout`, then:

```powershell
cd C:\spx-noon-breakout
py -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pip install "tastytrade>=13.2,<14"
venv\Scripts\python -m pytest -q
```

`tzdata` is required: Windows has no time-zone database without it. The tastytrade extra is also `pip install -e ".[tasty]"`.

3. Credentials. Sandbox and production are different OAuth apps. Never put them in the repo or in `config.json`.

Environment variables:

- `TASTYTRADE_SANDBOX_CLIENT_SECRET`
- `TASTYTRADE_SANDBOX_REFRESH_TOKEN`
- `TASTYTRADE_PROD_CLIENT_SECRET`
- `TASTYTRADE_PROD_REFRESH_TOKEN`

Or files under `%USERPROFILE%\secrets` (or `TASTYTRADE_CRED_DIR`): `.sb_secret`, `.sb_refresh`, `.prod_secret`, `.prod_refresh`.

Production data credentials are required at startup. Sandbox order mode does not read the production secret into the order session.

```powershell
setx SPX_NOON_HOME "C:\spx-noon-breakout"
```

Open a new PowerShell window afterwards. If scripts are blocked, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## Smoke test (read-only)

```powershell
venv\Scripts\python -m spx_noon check --broker tasty
```

The check connects, lists order-session accounts, and prints the SPX quote, 15-minute and 1-minute bar counts, the SPXW chain, VIX, and the safety banner. It does not place an order.

## Task Scheduler

Start **11:45 AM ET** on weekdays, only when you are logged on:

1. Create Task, "Run only when user is logged on".
2. Triggers: Weekly, Mon–Fri, 11:45 AM. Convert if the PC is not on Eastern.
3. Action: `powershell.exe`. Arguments: `-NoExit -ExecutionPolicy Bypass -File C:\spx-noon-breakout\run_session.ps1`. Add `-Mode live` only when you intend to send. Start in `C:\spx-noon-breakout`.
4. Conditions: "Wake the computer to run this task".

The script keeps Windows awake while the bot runs. Leave `tasty_orders` at `sandbox` until a live sandbox session looks right.

## End-of-day note (4:20 PM ET)

`run_wrap.ps1` reads `state\trades.csv` and writes `reports\wrap-<date>.md`. It does not place orders.
