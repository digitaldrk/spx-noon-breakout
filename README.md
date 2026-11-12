# Noon Breakout (plan v2, version B)

0DTE SPX debit vertical. The computer picks the broker:

| Machine | Broker | How orders go out |
| --- | --- | --- |
| Mac | IBKR TWS. **paper** is `127.0.0.1:7497` with account ids starting with `D`. **live** is real money on `127.0.0.1:7496` (TWS) or `4001` (IB Gateway) | `dry` (default) logs the ticket. `paper` sends it to the paper account. `live` sends real orders and needs the env latch. |
| Windows | **tastytrade** (`--broker tasty`, `tt`, or `tastytrade`; `auto` on Windows) | `dry` (default) and `alerts` never send. `live` auto-approves and sends to `tasty_orders` (default sandbox). `tasty_orders=production` is real money. |

A config file cannot switch brokers, and a config file alone can never enable real money. The bot trades version B only: the first qualifying 15-minute close from noon to 1:00 PM ET, one spread, long option nearest 75 delta, take-profit at +30%, flat two hours after that bar. Candle's 10:00–11:00 strategy is a different repo and is not used here.

**Mac:** copy `config.example.json` to `config.json`, keep TWS logged into paper on port 7497 with Read-Only API off, and use `run.sh`. `./run.sh paper` is the launch that transmits. State goes in `~/Documents/spx-noon-breakout/state`. For real money see [IBKR live mode](#ibkr-live-mode-real-money).

**Windows:** see [WINDOWS_SETUP.md](WINDOWS_SETUP.md). Copy `config.windows.example.json` to `config.json`. A bare `.\run_session.ps1` stays dry. `.\run_session.ps1 -Mode live` is the send mode. Tastytrade auto-approves. The price cap and 10 orders per minute still apply. `--broker schwab` keeps the `yes <ticketID>` gate.

## Configuration

`config.json` holds local overrides. Defaults are the fields on `Config` in `spx_noon/config.py`. Leave a key out and that default stays. Keys the loader does not know are ignored, including the `_comment`, `_rules`, and `_wrap` notes. The machine chooses the broker. A `broker` key in the file is ignored. A `dry_run` key is ignored too; `mode` is the only send setting.

Keep the real `config.json` on the machine. `.gitignore` excludes `config.json` and `tokens.json`. Schwab app key and secret stay in `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET`. The token file and any service-account json stay out of git.

| Machine | Copy | Where the launcher reads it |
| --- | --- | --- |
| Mac (IBKR paper) | `config.example.json` → `config.json` | `run.sh` loads `$SPX_NOON_HOME/config.json`. `SPX_NOON_HOME` defaults to `~/Documents/spx-noon-breakout`. |
| Windows (tastytrade) | `config.windows.example.json` → `config.json` | `run_session.ps1` loads `config.json` next to the script when the file exists. `-Mode` overrides `mode`. `-Broker` overrides `auto`. |

`python -m spx_noon` with no `--config` uses `SPX_NOON_CONFIG` when that variable is set, otherwise `config.json` in the repo root.

The 75-delta rule, the width, the $0.20 skip, the +30% target, and the two-hour flat are code, not config keys. They are in [docs/TRADING_PLAN.md](docs/TRADING_PLAN.md).

| Key | In the examples | Role |
| --- | --- | --- |
| `mode` | `dry` | Mac: `dry`, `alerts`, `paper`, or `live`. Windows tastytrade: `dry`, `alerts`, or `live`. `dry` and `alerts` never send. `paper` (Mac), `live` (Mac, with the env latch), and `live` (Windows) can. |
| `tasty_data` | `production` | Quote, bar, chain, and greek host. Missing production credentials fail startup. No fallback to sandbox data. |
| `tasty_orders` | `sandbox` | Order host. `production` is real money and requires `tasty_data=production`. Startup prints `DATA=<host> ORDERS=<host> REAL MONEY=YES/NO`. |
| `host`, `port`, `client_id` | `127.0.0.1`, `7497`, `41` | Mac TWS. Host must be localhost. Port must be 7497 for `dry`, `alerts`, and `paper`, or 7496/4001 for `live`. Client 41 leaves Candle (77) and Fly (31) alone. |
| `account` | `""` | Mac paper: paper account id (starts with `D`); blank uses the first account TWS shows, which must also start with `D`. Mac live: the live account id, required, must not start with `D`, and must match `SPX_NOON_LIVE_ACCOUNT`. Tastytrade: blank uses the only order-session account. |
| `base_dir` | `~/Documents/spx-noon-breakout` or `C:/spx-noon-breakout` | State, logs, and reports. Tastytrade order ids go in `state/tt_orders-<date>.json`. |
| `token_path` | `~/schwab/tokens.json` | Schwab token file (`--broker schwab`). `SCHWAB_TOKEN_PATH` replaces this value. |
| `approval_timeout_secs` | `300` | Schwab live: seconds to type `yes <id>` before that ticket is skipped. Tastytrade does not prompt. |

## IBKR live mode (real money)

Mac mode `live` sends real orders. Two things must agree before the process will start, and
**the config file alone can never turn this on**:

| Gate | Requirement |
| --- | --- |
| `mode` | `live` in `config.json` (or `./run.sh live`) |
| `host` | `127.0.0.1`, `localhost`, or `::1` |
| `port` | `7496` (TWS live) or `4001` (IB Gateway live). Nothing else, and never 7497. |
| `account` | set, non-empty, and **not** starting with `D`. Live never falls back to the first account TWS shows. |
| `SPX_NOON_ALLOW_LIVE` | exactly `1` in the environment. Not `true`, not `yes`, not `1 `. |
| `SPX_NOON_LIVE_ACCOUNT` | character-for-character equal to `account`. No strip, no casefold. |

If any of those fails the process refuses to start. The latch applies to IBKR only; tastytrade
and Schwab are unchanged.

After every connect and every reconnect the bot checks the accounts TWS reports: the configured
account must be there, must equal `SPX_NOON_LIVE_ACCOUNT`, must not start with `D`, and **no**
reported account may start with `D`. Any mismatch raises and the process exits; it is not
retried as a dropped connection. Paper, dry, and alerts keep the opposite rule — every reported
account must start with `D`.

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
- `SPX_NOON_ALLOW_LIVE=1` and `SPX_NOON_LIVE_ACCOUNT` exactly equal to config `account`. The
  config file cannot turn this on by itself.
- `./run.sh live` is the manual entry. `./deploy/install_launchd.sh live` is optional and does
  not replace the paper job.
- Live fills are in `state/trades-live.csv` and `reports/wrap-live-<date>.md`, not in the paper
  total.

```bash
export SPX_NOON_ALLOW_LIVE=1
export SPX_NOON_LIVE_ACCOUNT=U1234567     # exactly the config account
./run.sh live
```

### Separate books

Real money never mixes with the paper running total.

| | Paper / dry / alerts / sim / tastytrade / Schwab | IBKR live |
| --- | --- | --- |
| Trade log | `state/trades.csv` | `state/trades-live.csv` |
| Own position | `state/own_position.json` | `state/own_position-live.json` |
| Session report | `state/report_<date>.md` | `state/report-live-<date>.md` |
| 16:20 wrap | `run_wrap.sh` → `reports/wrap-<date>.md` | `run_wrap.sh live` → `reports/wrap-live-<date>.md` |

`python -m spx_noon wrap --book live` is the same thing by hand. It reads the live book only,
sends nothing, and does not need the latch while the config mode is still dry.

### launchd

`./deploy/install_launchd.sh live` installs **new** labels only, `com.derek.spxnoon.live` and
`com.derek.spxnoon.live.wrap`. It does not bootout or rewrite `com.derek.spxnoon` or
`com.derek.spxnoon.wrap`, and it refuses unless `SPX_NOON_LIVE_ACCOUNT` is set, non-empty, and
not starting with `D`. It writes that account and `SPX_NOON_ALLOW_LIVE=1` into the two new
plists only; no account id is committed to this repo. `./deploy/uninstall_launchd.sh` removes
all four labels when they are present.

## Shared account

Noon tracks only its own orders (IBKR `orderRef` `SPXNOON-…` on a Mac, `state/tt_orders-<date>.json` for tastytrade, `state/schwab_orders-<date>.json` for `--broker schwab`) and its own spread (`state/own_position.json`, or `state/own_position-live.json` for IBKR live). It does not count, modify, or cancel another bot's SPX position or order, it never adopts an order without the `SPXNOON-` prefix, and it does not scan the live account for other SPX positions.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json
./run.sh            # dry: logic and logs, no order
./run.sh paper      # TWS paper
./run.sh live       # REAL MONEY, only with the latch exported (see above)
.venv/bin/python -m pytest -q
.venv/bin/python -m spx_noon check
```

`python -m spx_noon check` stays a read-only paper check on 7497. It never dials 7496 or 4001, so a live `config.json` makes it refuse.

`./deploy/install_launchd.sh` schedules weekdays at 11:45 AM local (Eastern on this Mac) and a read-only wrap at 4:20 PM. The wrap writes `reports/wrap-<date>.md` from `state/trades.csv`. It does not connect and it does not send orders. With no arguments it only ever installs the paper/dry pair; `./deploy/install_launchd.sh live` is the separate opt-in.

Windows: [WINDOWS_SETUP.md](WINDOWS_SETUP.md).

## Trading plan

The bot implements version B in [docs/TRADING_PLAN.md](docs/TRADING_PLAN.md). Readings that the plan leaves open are in [docs/AMBIGUITIES.md](docs/AMBIGUITIES.md).

## Files

| Path | What it does |
| --- | --- |
| `spx_noon/strategy.py`, `pricing.py` | Breakout, 75-delta vertical, slippage skip, +30%, two-hour clock |
| `spx_noon/runner.py` | The session: one fill, GTC target, hard flat, trade log |
| `spx_noon/ib.py` | Mac broker. `ib_async` against TWS paper (7497) or, with the latch, live (7496/4001) |
| `spx_noon/tasty.py`, `check_tasty.py` | Windows broker. Production data session, sandbox orders unless configured. Auto-approves. |
| `spx_noon/schwab.py`, `schwabapi.py`, `guard.py` | `--broker schwab`. Refuses a live order with no approved ticket |
| `spx_noon/config.py` | Machine broker, the mode gate, the IBKR live latch, and the real-money banner |
| `run_wrap.sh`, `spx_noon/wrap_run.py` | 16:20 read-only note. `--book paper` or `--book live` |
| `config.example.json` | Mac template |
| `config.windows.example.json` | Windows tastytrade template |
| `run.sh`, `deploy/` | Mac launchers |
| `run_session.ps1` | Windows launcher. Default `-Mode dry` `-Broker auto` |
