# Noon Breakout (plan v2, version B or A1)

0DTE SPX debit vertical. `broker` in `config.json` names the broker — `ibkr` or `tasty`, and nothing else sets it:

| Machine | Broker | How orders go out |
| --- | --- | --- |
| Mac | **IBKR** TWS (`"broker": "ibkr"`). **paper** is `127.0.0.1:7497` with account ids starting with `D`. **live** is real money on `127.0.0.1:7496` (TWS) or `4001` (IB Gateway) | `dry` (default) logs the ticket. `paper` sends it to the paper account. `live` sends real orders to the configured non-`D` account. |
| Render Linux (hosted worker) | **tastytrade** (`"broker": "tasty"`) | `dry` and `alerts` never send. `live` auto-approves and sends to `tasty_orders` (default sandbox). `tasty_orders=production` is real money. |

A missing or blank `broker` refuses to start. On a Mac the config file is also the one switch between paper and real money: `mode`, `ibkr_port`, and `broker_account` in `config.json`, plus the TWS session you are logged into. There is one scheduled job either way. The bot takes the first qualifying 15-minute close from noon to 1:00 PM ET, one spread, flat two hours after that bar. `plan` in `config.json` picks the rules: `B` (the default) is 75 delta with a +30% take-profit, and `A1` is 80 delta with a +8-point exit and a morning filter — see [Trading plan](#trading-plan). Candle's 10:00–11:00 strategy is a different repo and is not used here.

**Mac:** copy `config.example.json` to `config.json`, keep TWS logged into paper on port 7497 with Read-Only API off, and use `run.sh`. `./run.sh paper` is the launch that transmits. State goes in `~/Documents/spx-noon-breakout/state`. For real money see [IBKR live mode](#ibkr-live-mode-real-money).

**Hosted (no computer left on):** see [RENDER.md](RENDER.md). One private Render background worker runs tastytrade from a container: real quotes, sandbox orders by default, state on a disk at `/data`. Its settings are plain variable names, the same set every bot uses on Render — `DATA`, `BROKER`, `MODE`, `TASTY_DATA`, `TASTY_ORDERS`, `BROKER_ACCOUNT` — plus the four `TASTYTRADE_*` secrets and `SPX_SHEETS_CREDENTIALS_JSON`. It changes nothing on this Mac.

**tastytrade by hand:** copy `config.tasty.example.json` to `config.json` and run `python -m spx_noon`. The copy stays `dry`; `"mode": "live"` is the send mode, and tastytrade auto-approves. The price cap and 10 orders per minute still apply. There is no scheduled local tastytrade job — the hosted worker is the always-on path.

## Configuration

`config.json` holds local overrides. Defaults are the fields on `Config` in `spx_noon/config.py`. Leave a key out and that default stays. Keys the loader does not know are ignored, including the `_comment`, `_rules`, and `_wrap` notes. The file's `broker` key names the broker: `ibkr` or `tasty`, after stripping whitespace and lower-casing. No launcher, flag, or platform check sets it, and a missing or blank value refuses to start. On the Render worker a non-blank `BROKER` overrides that one key. A `dry_run` key is ignored; `mode` is the only send setting.

Keep the real `config.json` on the machine. `.gitignore` excludes `config.json` and `tokens.json`. Tastytrade credentials stay in environment variables or files under `secrets/`, never in the repo. Any token file and any service-account json stay out of git.

| Machine | Copy | Where the launcher reads it |
| --- | --- | --- |
| Mac (IBKR paper) | `config.example.json` → `config.json` (`"broker": "ibkr"`) | `run.sh` loads `$SPX_NOON_HOME/config.json`. `SPX_NOON_HOME` defaults to `~/Documents/spx-noon-breakout`. |
| tastytrade by hand | `config.tasty.example.json` → `config.json` (`"broker": "tasty"`) | `python -m spx_noon` reads `SPX_NOON_CONFIG` when it is set, otherwise `config.json` in the repo root. |
| Render Linux (tastytrade) | `config.render.example.json` is seeded onto the disk as `/data/config.json` | the worker resolves its six plain variables over that file. See [RENDER.md](RENDER.md). |

`python -m spx_noon` with no `--config` uses `SPX_NOON_CONFIG` when that variable is set, otherwise `config.json` in the repo root.

The delta, the width, the $0.20 skip, the +30% target, A1's 8 points, and the two-hour flat are code, not config keys. `plan` is the one trading rule that is a key. They are in [docs/TRADING_PLAN.md](docs/TRADING_PLAN.md).

| Key | In the examples | Role |
| --- | --- | --- |
| `broker` | `ibkr` (Mac), `tasty` (tastytrade, Render) | The broker: `ibkr` or `tasty`. Missing or blank refuses to start, and so does any other value. The Render worker's `BROKER` overrides it. |
| `plan` | `B` | The trading plan: `B` or `A1`. A missing key is `B`. Anything else refuses to start. There is no `PLAN` environment variable, including on the Render worker. |
| `mode` | `dry` | Mac/IBKR: `dry`, `alerts`, `paper`, or `live`. Tastytrade: `dry`, `alerts`, or `live`. `dry` and `alerts` never send. `paper` (Mac), `live` (Mac, real money), and `live` (tastytrade) can. |
| `tasty_data` | `production` | Quote, bar, chain, and greek host. Missing production credentials fail startup. No fallback to sandbox data. |
| `tasty_orders` | `sandbox` | Order host. `production` is real money and requires `tasty_data=production`. Startup prints `DATA=<host> ORDERS=<host> REAL MONEY=YES/NO`. |
| `ibkr_host`, `ibkr_port`, `ibkr_client_id` | `127.0.0.1`, `7497`, `41` | Mac TWS. Host must be localhost. Port must be 7497 for `dry`, `alerts`, and `paper`, or 7496/4001 for `live`. Client 41 leaves Candle (77) and Fly (31) alone. |
| `broker_account` | `""` | Mac paper: paper account id (starts with `D`); blank uses the first account TWS shows, which must also start with `D`. Mac live: the live account id, required, must not start with `D`, and TWS must report it. Tastytrade: blank uses the only order-session account. |
| `base_dir` | `~/Documents/spx-noon-breakout`; left out of the tasty and Render examples | State, logs, and reports. Left out, `SPX_NOON_HOME` fills it, then the code default (`~/Documents/spx-noon-breakout` for `ibkr`, `~/spx-noon-breakout` for `tasty`); the hosted worker uses `DATA`. Tastytrade order ids go in `state/tt_orders-<date>.json`. |

## IBKR live mode (real money)

Mac mode `live` sends real orders. There is one scheduled job and one config; paper and live are
two settings of it. The switch is `config.json` plus the TWS session you are logged into:

| | Paper | Live (real money) |
| --- | --- | --- |
| `mode` | `paper` | `live` |
| `ibkr_port` | `7497` | `7496` (TWS) or `4001` (IB Gateway) |
| `broker_account` | starts with `D`, or blank for the first `D` account TWS shows | set, and **not** starting with `D`. No fallback to the first account TWS shows. |
| TWS | logged into paper | logged into the live account |

`ibkr_host` must be `127.0.0.1`, `localhost`, or `::1` in both. If any of those fails the process
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
- `config.json`: `"mode": "live"`, `"ibkr_port": 7496` (or `4001`), `"broker_account"` set to the live id.
  The same 11:45 job then runs live; there is no second job to install.
- `./run.sh live` is the manual one-shot. It overrides `mode` for that launch only and still
  needs the live port, the live account, and the live TWS session.
- Live fills are in `state/trades-live.csv` and `reports/wrap-live-<date>.md`, not in the paper
  total.

```jsonc
// config.json — the whole switch
"mode": "live", "ibkr_host": "127.0.0.1", "ibkr_port": 7496, "broker_account": "U1234567"
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

The two markdown files stay separate. The sheet row below does not: it is one row per date for
this bot, so a later wrap of the other book on the same date replaces it.

### The 16:20 sheet row

After the markdown report is on disk, the wrap upserts **one** daily row for Boi `High Noon` on
the Google Sheet Candle (`spx-breakout`) and Fly (`spx-fly`) already share,
`sheets_spreadsheet_id`, default `1tnMlDTidwCYG3_H2Z-lBcIUG5KOX4tgbe8AiRopATsU`. The columns are
theirs and are not extended here:

| Column | What this bot writes |
| --- | --- |
| `Date` | the session date, the same one in the report's filename |
| `Boi` | `High Noon`, always |
| `Traded` | `yes` when the selected book has a filled or closed row that date, else `no` |
| `P&L after fees` | that row's `pnl`, which is already net of fees. Several closed rows are summed; a blank `pnl` leaves the cell blank rather than claiming a zero |
| `Worst` | empty. The trade log has no worst or drawdown column yet |
| `What happened` | direction, both strikes, entry fill, exit fill, fees, and exit reason. A `skip_no_fill` or `skip_morning` day says so; a day with no rows says `No rows for today.` |

The key is `(Date, Boi)`, so a second wrap on the same date replaces that row and never
duplicates it, and Candle's and Fly's rows on the tab are left exactly as they are. The
row goes on the `Derek Comp` tab, on this Mac and on the Render worker. There is no tab
setting.

The key is a Google service-account json. The first of these that is set wins:

| Where | Notes |
| --- | --- |
| `SPX_SHEETS_CREDENTIALS_JSON` | the whole json, outer braces included. Used in memory, never written to disk, never logged or printed. This is how the Render worker gets it ([RENDER.md](RENDER.md)) |
| `sheets_credentials` in `config.json` | a path to the key file. Blank means the default below |
| `SPX_SHEETS_CREDENTIALS` | a path to the key file |
| `GOOGLE_APPLICATION_CREDENTIALS` | a path to the key file |
| `<home>/state/sheets-service-account.json` | the default path. Gitignored by name and by `state/` |

The key itself is never a `config.json` field, and nothing copies it into
`state/service-config.json`. Share the sheet with the service account's `client_email`, and
install the client libraries where the wrap runs:

```bash
.venv/bin/pip install -e ".[sheets]"
```

A sheet problem is quiet by design: a missing key, a json blob that will not parse, the Google
packages not installed, or an API error each leave the markdown report in place, append one
status line to it, print that line, and **exit 0**. No status line quotes the json, a
`private_key`, or a key file's contents. The 16:20 job always tries the sheet; there is no flag
to skip it.

### launchd

`./deploy/install_launchd.sh` takes **no arguments** and installs exactly two labels,
`com.derek.spxnoon` (11:45, `run.sh`) and `com.derek.spxnoon.wrap` (16:20, `run_wrap.sh`). Both
follow `config.json`, so there is nothing to reinstall when you switch between paper and live.
Any argument, `live` included, prints usage and exits 2; no account id is ever written into a
plist.

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

The tests ignore this machine's broker setup: they read no `TASTYTRADE_*` credential, no `~/secrets`, and no `config.json` of yours, and they never connect to tastytrade or to TWS, so `.venv/bin/python -m pytest -q` gives the same result whether the machine is set up for IBKR, for tastytrade, or for neither.

`python -m spx_noon check` follows `config.json`: an `ibkr` file is a read-only paper check on 7497, and a `tasty` file checks tastytrade. It never dials 7496 or 4001, so a live IBKR `config.json` makes it refuse.

`./deploy/install_launchd.sh` schedules weekdays at 11:45 AM local (Eastern on this Mac) and a read-only wrap at 4:20 PM. Both follow `config.json`. The wrap places no orders; it writes `reports/wrap-<date>.md` from `state/trades.csv`, or `reports/wrap-live-<date>.md` from `state/trades-live.csv` when the config is IBKR live, and then upserts one `High Noon` row on the shared Google Sheet (see [The 16:20 sheet row](#the-1620-sheet-row)). Without a service-account key it records that the sheet was not updated and still exits 0.

Hosted tastytrade: [RENDER.md](RENDER.md).

## Trading plan

The bot implements version B in [docs/TRADING_PLAN.md](docs/TRADING_PLAN.md), and plan A1 behind one key. Readings that the plan leaves open are in [docs/AMBIGUITIES.md](docs/AMBIGUITIES.md).

`"plan": "B"` (the default, and what a missing key means) or `"plan": "A1"`. One code base, one job; each machine picks its plan in its own `config.json`. Everything both plans share stays the same: the 12:00–1:00 PM ET closes, the first qualifying bar, one fill per day, calls before puts, `W = max(10, floor(EM / 5) × 5)`, the limit at mid, the $0.20-per-side skip, and the flat two hours after the entry bar.

A1 changes exactly four things:

| | B | A1 |
| --- | --- | --- |
| Signal | a 15-minute close from 9:45 on beyond the level is the trigger; the window close must still be beyond the level and beyond that trigger bar's low (calls) or high (puts) | the day's **first** noon-window close beyond the prior day's high (calls) or low (puts). No trigger bar: a fresh break on the window close itself is the signal. |
| Morning | no filter | skip the whole day when any 15-minute close from 10:15 through 11:00 ET was already beyond the prior day's high or low. Equal to the level does not skip, and a 9:45 break does not either. |
| Long leg | nearest **0.75** delta, in the money | nearest **0.80** delta, in the money |
| Exit | GTC at debit × 1.30, else flat at two hours | **+8 SPX points** in the trade direction, else flat at two hours. No premium target and no stop. |

A skipped morning sends no order and writes one `skip_morning` row in the trade log. An A1 exit on the points reads `points` there. The 0.75, the 0.80, and the 8 points are code; `plan` is the only key.

## Files

| Path | What it does |
| --- | --- |
| `spx_noon/strategy.py`, `pricing.py` | Breakout, the 75- or 80-delta vertical, slippage skip, +30%, A1's morning filter and +8 points, two-hour clock |
| `spx_noon/runner.py` | The session: one fill, the plan's exit (GTC target or +8 points), hard flat, trade log |
| `spx_noon/ib.py` | Mac broker. `ib_async` against TWS paper (7497) or live (7496/4001) |
| `spx_noon/tasty.py`, `check_tasty.py` | tastytrade broker, used by the hosted Render Linux worker and by a local `tasty` config. Production data session, sandbox orders unless configured. Auto-approves. |
| `spx_noon/guard.py` | In-process ticket guard. Refuses a live order with no approved ticket, a price over the cap, or more than 10 requests a minute |
| `spx_noon/config.py` | The file's broker, the mode gate, the IBKR live port/account gate, and the real-money banner |
| `run_wrap.sh`, `spx_noon/wrap_run.py` | 16:20 read-only note. `--book paper` or `--book live` |
| `spx_noon/sheetsink.py` | The 16:20 wrap's one `High Noon` row on the shared sheet: the row, the tab rule, the `(Date, Boi)` upsert, and the credential order. Never raises; a sheet failure is one status line |
| `config.example.json` | Mac IBKR template |
| `config.tasty.example.json` | tastytrade template |
| `spx_noon/service.py` | Hosted worker supervisor: reads `DATA`, `BROKER`, `MODE`, `TASTY_DATA`, `TASTY_ORDERS`, `BROKER_ACCOUNT`, waits for 11:45, spawns the session, restarts a crash, runs the 16:20 wrap. [RENDER.md](RENDER.md) |
| `render.yaml`, `Dockerfile`, `config.render.example.json` | Render blueprint, image, and the seed copied to the disk once |
| `run.sh`, `deploy/` | Mac launchers and the two launchd labels |
