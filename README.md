# Noon Breakout (plan v2, version B)

0DTE SPX debit vertical. The computer picks the broker:

| Machine | Broker | How orders go out |
| --- | --- | --- |
| Mac | IBKR TWS. **paper** is `127.0.0.1:7497` with account ids starting with `D`. **live** is real money on `127.0.0.1:7496` (TWS) or `4001` (IB Gateway) | `dry` (default) logs the ticket. `paper` sends it to the paper account. `live` sends real orders to the configured non-`D` account. |
| Windows | **tastytrade** (`--broker tasty`, `tt`, or `tastytrade`; `auto` on Windows) | `dry` (default) and `alerts` never send. `live` auto-approves and sends to `tasty_orders` (default sandbox). `tasty_orders=production` is real money. |

A config file cannot switch brokers. On a Mac it is the one switch between paper and real money: `mode`, `port`, and `account` in `config.json`, plus the TWS session you are logged into. There is one scheduled job either way. The bot trades version B only: the first qualifying 15-minute close from noon to 1:00 PM ET, one spread, long option nearest 75 delta, take-profit at +30%, flat two hours after that bar. Candle's 10:00–11:00 strategy is a different repo and is not used here.

**Mac:** copy `config.example.json` to `config.json`, keep TWS logged into paper on port 7497 with Read-Only API off, and use `run.sh`. `./run.sh paper` is the launch that transmits. State goes in `~/Documents/spx-noon-breakout/state`. For real money see [IBKR live mode](#ibkr-live-mode-real-money).

**Windows:** see [WINDOWS_SETUP.md](WINDOWS_SETUP.md). Copy `config.windows.example.json` to `config.json`. A bare `.\run_session.ps1` stays dry. `.\run_session.ps1 -Mode live` is the send mode. Tastytrade auto-approves. The price cap and 10 orders per minute still apply.

## Configuration

`config.json` holds local overrides. Defaults are the fields on `Config` in `spx_noon/config.py`. Leave a key out and that default stays. Keys the loader does not know are ignored, including the `_comment`, `_rules`, and `_wrap` notes. The machine chooses the broker. A `broker` key in the file is ignored. A `dry_run` key is ignored too; `mode` is the only send setting.

Keep the real `config.json` on the machine. `.gitignore` excludes `config.json` and `tokens.json`. Tastytrade credentials stay in environment variables or files under `secrets/`, never in the repo. Any token file and any service-account json stay out of git.

| Machine | Copy | Where the launcher reads it |
| --- | --- | --- |
| Mac (IBKR paper) | `config.example.json` → `config.json` | `run.sh` loads `$SPX_NOON_HOME/config.json`. `SPX_NOON_HOME` defaults to `~/Documents/spx-noon-breakout`. |
| Windows (tastytrade) | `config.windows.example.json` → `config.json` | `run_session.ps1` loads `config.json` next to the script when the file exists. `-Mode` overrides `mode`. `-Broker` overrides `auto`. |

`python -m spx_noon` with no `--config` uses `SPX_NOON_CONFIG` when that variable is set, otherwise `config.json` in the repo root.

The 75-delta rule, the width, the $0.20 skip, the +30% target, and the two-hour flat are code, not config keys. They are in [docs/TRADING_PLAN.md](docs/TRADING_PLAN.md).

| Key | In the examples | Role |
| --- | --- | --- |
| `mode` | `dry` | Mac: `dry`, `alerts`, `paper`, or `live`. Windows tastytrade: `dry`, `alerts`, or `live`. `dry` and `alerts` never send. `paper` (Mac), `live` (Mac, real money), and `live` (Windows) can. |
| `tasty_data` | `production` | Quote, bar, chain, and greek host. Missing production credentials fail startup. No fallback to sandbox data. |
| `tasty_orders` | `sandbox` | Order host. `production` is real money and requires `tasty_data=production`. Startup prints `DATA=<host> ORDERS=<host> REAL MONEY=YES/NO`. |
| `host`, `port`, `client_id` | `127.0.0.1`, `7497`, `41` | Mac TWS. Host must be localhost. Port must be 7497 for `dry`, `alerts`, and `paper`, or 7496/4001 for `live`. Client 41 leaves Candle (77) and Fly (31) alone. |
| `account` | `""` | Mac paper: paper account id (starts with `D`); blank uses the first account TWS shows, which must also start with `D`. Mac live: the live account id, required, must not start with `D`, and TWS must report it. Tastytrade: blank uses the only order-session account. |
| `base_dir` | `~/Documents/spx-noon-breakout` or `C:/spx-noon-breakout` | State, logs, and reports. Tastytrade order ids go in `state/tt_orders-<date>.json`. |

## IBKR live mode (real money)

Mac mode `live` sends real orders. There is one scheduled job and one config; paper and live are
two settings of it. The switch is `config.json` plus the TWS session you are logged into:

| | Paper | Live (real money) |
| --- | --- | --- |
| `mode` | `paper` | `live` |
| `port` | `7497` | `7496` (TWS) or `4001` (IB Gateway) |
| `account` | starts with `D`, or blank for the first `D` account TWS shows | set, and **not** starting with `D`. No fallback to the first account TWS shows. |
| TWS | logged into paper | logged into the live account |

`host` must be `127.0.0.1`, `localhost`, or `::1` in both. If any of those fails the process
refuses to start. There are no environment variables in this gate; tastytrade is unchanged.

After every connect and every reconnect the bot checks the accounts TWS reports: the configured
account must be there, must not start with `D`, and **no** reported account may start with `D`.
Any mismatch raises and the process exits; it is not retried as a dropped connection. Paper,
dry, and alerts keep the opposite rule — every reported account must start with `D`.

Once the account is verified the session prints and logs one banner, even on a day it will not
trade:

```
BROKER=ibkr MODE=live ACCOUNT=4321 PORT=7496 REAL MONEY=YES
```

Only the last four characters of the account appear. Paper, dry, and alerts print
`REAL MONEY=NO`. Every IBKR order log line carries the same `REAL MONEY=YES/NO` flag.

### Operator checklist

- Turn off the Candle and Fly launchd jobs while High Noon runs live.
- TWS (or IB Gateway) logged into the **live** account, API enabled, Read-Only API off, socket
  port 7496 (Gateway 4001).
- The live account has SPX index + SPX options market data subscriptions and options trading
  permissions for spreads.
- First live day: watch the banner and the first order ticket.
- `config.json`: `"mode": "live"`, `"port": 7496` (or `4001`), `"account"` set to the live id.
  The same 11:45 job then runs live; there is no second job to install.
- `./run.sh live` is the manual one-shot. It overrides `mode` for that launch only and still
  needs the live port, the live account, and the live TWS session.
- Live fills are in `state/trades-live.csv` and `reports/wrap-live-<date>.md`, not in the paper
  total.

```jsonc
// config.json — the whole switch
"mode": "live", "host": "127.0.0.1", "port": 7496, "account": "U1234567"
```

### Separate books

Real money never mixes with the paper running total.

| | Paper / dry / alerts / sim / tastytrade | IBKR live |
| --- | --- | --- |
| Trade log | `state/trades.csv` | `state/trades-live.csv` |
| Own position | `state/own_position.json` | `state/own_position-live.json` |
| Session report | `state/report_<date>.md` | `state/report-live-<date>.md` |
| 16:20 wrap | `reports/wrap-<date>.md` | `reports/wrap-live-<date>.md` |

The one 16:20 job runs `run_wrap.sh` with no argument, so the book follows the loaded config: an
IBKR live config writes the live wrap, everything else writes the paper wrap. `run_wrap.sh live`
and `python -m spx_noon wrap --book live` are the same thing by hand — they read the live book
only, send nothing, and work from any config, including a dry one.

### launchd

`./deploy/install_launchd.sh` takes **no arguments** and installs exactly two labels,
`com.derek.spxnoon` (11:45, `run.sh`) and `com.derek.spxnoon.wrap` (16:20, `run_wrap.sh`). Both
follow `config.json`, so there is nothing to reinstall when you switch between paper and live.
Any argument, `live` included, prints usage and exits 2; no account id is ever written into a
plist.

Upgrading from the old two-job setup: run `./deploy/install_launchd.sh` once. It boots out and
deletes `com.derek.spxnoon.live` and `com.derek.spxnoon.live.wrap` if an older install left them
scheduled, so the stale real-money job cannot fire at 11:45 next to this one. It prints
`removed retired …` for each. `./deploy/uninstall_launchd.sh` removes all four labels.

## Shared account

Noon tracks only its own orders (IBKR `orderRef` `SPXNOON-…` on a Mac, `state/tt_orders-<date>.json` for tastytrade) and its own spread (`state/own_position.json`, or `state/own_position-live.json` for IBKR live). It does not count, modify, or cancel another bot's SPX position or order, it never adopts an order without the `SPXNOON-` prefix, and it does not scan the live account for other SPX positions.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json
./run.sh            # follows config.json mode (the example is dry)
./run.sh paper      # one-shot override: TWS paper
./run.sh live       # one-shot override: REAL MONEY (see above for the config it needs)
.venv/bin/python -m pytest -q
.venv/bin/python -m spx_noon check
```

`python -m spx_noon check` stays a read-only paper check on 7497. It never dials 7496 or 4001, so a live `config.json` makes it refuse.

`./deploy/install_launchd.sh` schedules weekdays at 11:45 AM local (Eastern on this Mac) and a read-only wrap at 4:20 PM. Both follow `config.json`. The wrap does not connect and does not send orders; it writes `reports/wrap-<date>.md` from `state/trades.csv`, or `reports/wrap-live-<date>.md` from `state/trades-live.csv` when the config is IBKR live.

Windows: [WINDOWS_SETUP.md](WINDOWS_SETUP.md).

## Trading plan

The bot implements version B in [docs/TRADING_PLAN.md](docs/TRADING_PLAN.md). Readings that the plan leaves open are in [docs/AMBIGUITIES.md](docs/AMBIGUITIES.md).

## Files

| Path | What it does |
| --- | --- |
| `spx_noon/strategy.py`, `pricing.py` | Breakout, 75-delta vertical, slippage skip, +30%, two-hour clock |
| `spx_noon/runner.py` | The session: one fill, GTC target, hard flat, trade log |
| `spx_noon/ib.py` | Mac broker. `ib_async` against TWS paper (7497) or live (7496/4001) |
| `spx_noon/tasty.py`, `check_tasty.py` | Windows broker. Production data session, sandbox orders unless configured. Auto-approves. |
| `spx_noon/guard.py` | In-process ticket guard. Refuses a live order with no approved ticket, a price over the cap, or more than 10 requests a minute |
| `spx_noon/config.py` | Machine broker, the mode gate, the IBKR live port/account gate, and the real-money banner |
| `run_wrap.sh`, `spx_noon/wrap_run.py` | 16:20 read-only note. `--book paper` or `--book live` |
| `config.example.json` | Mac template |
| `config.windows.example.json` | Windows tastytrade template |
| `run.sh`, `deploy/` | Mac launchers |
| `run_session.ps1` | Windows launcher. Default `-Mode dry` `-Broker auto` |
