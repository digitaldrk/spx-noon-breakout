# Windows setup (Schwab Trader API, confirm-before-send)

This is the Windows machine. A Mac runs IBKR paper through `run.sh` and `deploy/install_launchd.sh`, using `config.example.json`. This file does not apply there. On this PC, copy `config.windows.example.json` to `config.json`.

## What it does

Noon Breakout, plan v2 version B: one 0DTE SPX debit vertical on the first qualifying 15-minute close from noon to 1:00 PM ET. Long strike nearest 75 delta. Take-profit at +30% of the debit. Flat two hours after the entry bar.

| Command | Orders |
| --- | --- |
| `.\run_session.ps1` | Dry. Nothing is sent. |
| `.\run_session.ps1 -Mode alerts` | Nothing is sent. The ticket is printed and toasted. Place it yourself if you want it. |
| `.\run_session.ps1 -Mode live` | The only mode that can send. Schwab `previewOrder` runs first. The order is sent only after you type `yes N120015` (that ticket's id) in this window. A timeout (300 s, `approval_timeout_secs`) or any other answer skips it. One approval covers that spread: the entry, its GTC take-profit, and the two-hour flat. |

Safety (`spx_noon/guard.py`, `spx_noon/schwab.py`):

- Order calls are refused without an approved ticket id, and an entry above the approved cap (the mid limit; this bot does not chase) is refused.
- At most 10 order requests per minute.
- Dry and alerts cannot send. Approvals are refused in those modes too.
- Orders this process did not record are left alone. Refs live in `state\schwab_orders-<date>.json`.

## Config file

```powershell
copy config.windows.example.json config.json
```

`config.json` stays on this PC. App key and secret stay in `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET`. The example in git has no secrets. `run_session.ps1 -Mode` overrides `mode`. With no `-Mode`, the script runs **dry**.

| Key | Example | Role |
| --- | --- | --- |
| `mode` | `dry` | `dry` and `alerts` never send. `live` sends only after `yes <ticketID>`. |
| `account` | `705` | Last digits of the Schwab account. They must match exactly one account. |
| `token_path` | `~/schwab/tokens.json` | Shared refresh-token file. `SCHWAB_TOKEN_PATH` replaces it. |
| `base_dir` | `C:/spx-noon-breakout` | State, logs, and reports. |
| `approval_timeout_secs` | `300` | Seconds to type `yes <id>`. |

## One-time install

1. Install Python 3.11 or newer and tick "Add python.exe to PATH".
2. Unzip to `C:\spx-noon-breakout`, then:

```powershell
cd C:\spx-noon-breakout
py -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python -m pytest -q
```

`tzdata` is required: Windows has no time-zone database without it.

3. Set the app credentials as user environment variables (never in the repo):

```powershell
setx SCHWAB_APP_KEY "..."
setx SCHWAB_APP_SECRET "..."
setx SPX_NOON_HOME "C:\spx-noon-breakout"
setx SCHWAB_TOKEN_PATH "$env:USERPROFILE\schwab\tokens.json"
```

Open a new PowerShell window afterwards. If scripts are blocked, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

4. `copy config.windows.example.json config.json`. Keep `"account": "705"`. Leave `"mode": "dry"` unless you are about to pass `-Mode live`.

## Token file

- The bot reads `SCHWAB_TOKEN_PATH` (default `~\schwab\tokens.json`). Treat it like a password.
- It refreshes the access token and writes the file back. It never revokes the token and never runs the browser login.
- A new browser login in one place can invalidate the other copies. Log in once, then copy the file.
- The refresh token lasts about 7 days.

## Smoke test (read-only)

```powershell
venv\Scripts\python -m spx_noon check
venv\Scripts\python -m spx_noon check --preview-sample
```

`--preview-sample` calls `previewOrder` only. It does not place the order.

## Task Scheduler

Start **11:45 AM ET** on weekdays, only when you are logged on, in a visible window, because live mode needs the keyboard:

1. Create Task, "Run only when user is logged on".
2. Triggers: Weekly, Mon–Fri, 11:45 AM. Convert if the PC is not on Eastern.
3. Action: `powershell.exe`. Arguments: `-NoExit -ExecutionPolicy Bypass -File C:\spx-noon-breakout\run_session.ps1`. Add `-Mode live` only when Tod is at the keyboard. Start in `C:\spx-noon-breakout`.
4. Conditions: "Wake the computer to run this task".

The script keeps Windows awake while the bot runs. Do not share this console with another bot: each live bot needs its own window for `yes <id>`.

## End-of-day note (4:20 PM ET)

`run_wrap.ps1` reads `state\trades.csv` and writes `reports\wrap-<date>.md`. It does not place orders and it does not need a yes-prompt.
