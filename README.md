# Noon Breakout (plan v2, version B)

0DTE SPX debit vertical. The computer picks the broker:

| Machine | Broker | How orders go out |
| --- | --- | --- |
| Mac | IBKR TWS **paper**, `127.0.0.1:7497` only, account ids starting with `D` | `dry` (default) logs the ticket. `paper` sends it to the paper account. |
| Windows | Tod's **Schwab** account | `dry` (default) and `alerts` never send. `live` sends only after Tod types `yes <ticketID>`. |

A config file cannot switch brokers. The bot trades version B only: the first qualifying 15-minute close from noon to 1:00 PM ET, one spread, long option nearest 75 delta, take-profit at +30%, flat two hours after that bar. Candle's 10:00–11:00 strategy is a different repo and is not used here.

**Mac:** copy `config.example.json` to `config.json`, keep TWS logged into paper on port 7497 with Read-Only API off, and use `run.sh`. `./run.sh paper` is the launch that transmits. State goes in `~/Documents/spx-noon-breakout/state`.

**Windows:** see [WINDOWS_SETUP.md](WINDOWS_SETUP.md). Copy `config.windows.example.json` to `config.json`. A bare `.\run_session.ps1` stays dry. `.\run_session.ps1 -Mode live` is the only send mode, and `spx_noon/guard.py` still refuses every Schwab order without an approved ticket.

## Configuration

`config.json` holds local overrides. Defaults are the fields on `Config` in `spx_noon/config.py`. Leave a key out and that default stays. Keys the loader does not know are ignored, including the `_comment`, `_rules`, and `_wrap` notes. The machine chooses the broker. A `broker` key in the file is ignored. A `dry_run` key is ignored too; `mode` is the only send setting.

Keep the real `config.json` on the machine. `.gitignore` excludes `config.json` and `tokens.json`. Schwab app key and secret stay in `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET`. The token file and any service-account json stay out of git.

| Machine | Copy | Where the launcher reads it |
| --- | --- | --- |
| Mac (IBKR paper) | `config.example.json` → `config.json` | `run.sh` loads `$SPX_NOON_HOME/config.json`. `SPX_NOON_HOME` defaults to `~/Documents/spx-noon-breakout`. |
| Windows (Schwab) | `config.windows.example.json` → `config.json` | `run_session.ps1` loads `config.json` next to the script when the file exists. `-Mode` overrides `mode`. |

`python -m spx_noon` with no `--config` uses `SPX_NOON_CONFIG` when that variable is set, otherwise `config.json` in the repo root.

The 75-delta rule, the width, the $0.20 skip, the +30% target, and the two-hour flat are code, not config keys. They are in [docs/TRADING_PLAN.md](docs/TRADING_PLAN.md).

| Key | In the examples | Role |
| --- | --- | --- |
| `mode` | `dry` | Mac: `dry`, `alerts`, or `paper`. Windows: `dry`, `alerts`, or `live`. `dry` and `alerts` never send. `paper` (Mac) and `live` (Windows) can. |
| `host`, `port`, `client_id` | `127.0.0.1`, `7497`, `41` | Mac TWS paper. Host must be localhost. Port must be 7497. Client 41 leaves Candle (77) and Fly (31) alone. |
| `account` | `""` (Mac), `"705"` (Windows) | Mac: paper account id (starts with `D`). Blank uses the first account TWS shows. Windows: last digits, and they must match exactly one account. |
| `base_dir` | `~/Documents/spx-noon-breakout` or `C:/spx-noon-breakout` | State, logs, and reports. |
| `token_path` | `~/schwab/tokens.json` | Windows token file. `SCHWAB_TOKEN_PATH` replaces this value. |
| `approval_timeout_secs` | `300` | Windows live: seconds to type `yes <id>` before that ticket is skipped. |

## Shared account

Noon tracks only its own orders (IBKR `orderRef` `SPXNOON-…` on a Mac, `state/schwab_orders-<date>.json` on Windows) and its own spread (`state/own_position.json`). It does not count, modify, or cancel another bot's SPX position or order.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json
./run.sh            # dry: logic and logs, no order
./run.sh paper      # TWS paper
.venv/bin/python -m pytest -q
.venv/bin/python -m spx_noon check
```

`./deploy/install_launchd.sh` schedules weekdays at 11:45 AM local (Eastern on this Mac) and a read-only wrap at 4:20 PM. The wrap writes `reports/wrap-<date>.md` from `state/trades.csv`. It does not connect and it does not send orders.

Windows: [WINDOWS_SETUP.md](WINDOWS_SETUP.md).

## Trading plan

The bot implements version B in [docs/TRADING_PLAN.md](docs/TRADING_PLAN.md). Readings that the plan leaves open are in [docs/AMBIGUITIES.md](docs/AMBIGUITIES.md).

## Files

| Path | What it does |
| --- | --- |
| `spx_noon/strategy.py`, `pricing.py` | Breakout, 75-delta vertical, slippage skip, +30%, two-hour clock |
| `spx_noon/runner.py` | The session: one fill, GTC target, hard flat, trade log |
| `spx_noon/ib.py` | Mac broker. `ib_async` against TWS paper |
| `spx_noon/schwab.py`, `schwabapi.py`, `guard.py` | Windows broker. Refuses a live order with no approved ticket |
| `spx_noon/config.py` | Machine broker and the mode gate |
| `config.example.json` | Mac template |
| `config.windows.example.json` | Windows template |
| `run.sh`, `deploy/` | Mac launchers |
| `run_session.ps1` | Windows launcher. Default `-Mode dry` |
