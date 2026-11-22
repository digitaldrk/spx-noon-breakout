# Running High Noon on Render

One private background worker, always awake, tastytrade only. No computer stays
on, nothing listens on a port, and there is no public URL or status page. State
lives on a 1 GB disk at `/data`, so a restart in the middle of the day still
sees today's open position.

The default is **real quotes, sandbox orders**. The worker sends: `mode` is
`live`, which is what transmits, and the orders go to the tastytrade sandbox
host. The startup banner says `REAL MONEY=NO` until you move orders to
production yourself.

## Deploy

1. Push this repo to GitHub (the blueprint reads `render.yaml` from the repo root).
2. Render dashboard → **New → Blueprint** → pick this repo → **Apply**. It
   creates one worker named `spx-noon` on the Starter plan in Ohio, with the
   disk `spx-noon-data` mounted at `/data`.
3. Render will ask for the six secret values before the first deploy finishes.
   Paste them in the dashboard (**the worker → Environment**). They are never in
   the repo, the image, or a log line:

   | Variable | What it is |
   | --- | --- |
   | `BROKER_ACCOUNT` | tastytrade account to trade. Blank only works when the order session has exactly one account. |
   | `TASTYTRADE_PROD_CLIENT_SECRET` | production OAuth client secret (quotes) |
   | `TASTYTRADE_PROD_REFRESH_TOKEN` | production refresh token (quotes) |
   | `TASTYTRADE_SANDBOX_CLIENT_SECRET` | sandbox OAuth client secret (orders) |
   | `TASTYTRADE_SANDBOX_REFRESH_TOKEN` | sandbox refresh token (orders) |
   | `SPX_SHEETS_CREDENTIALS_JSON` | the whole Google service-account json, outer braces included, for the 16:20 wrap's row on the shared sheet. Optional: leave it unset and the wrap writes its report and records that the sheet was not updated. |

   Production credentials are needed even in the default setup: quotes, bars,
   the chain, and greeks come from the production host and never fall back to
   sandbox data.

   `SPX_SHEETS_CREDENTIALS_JSON` is **not** one of the six worker settings in
   the next section. No code in `spx_noon/service.py` reads it: the supervisor
   hands the wrap child its whole environment, so the value reaches the wrap
   without ever being copied into `/data/config.json` or
   `/data/state/service-config.json`. Do not put the json in either file.
4. Deploy. On the first boot the worker copies `config.render.example.json` to
   `/data/config.json` and prints one `boot` line. It never overwrites that file
   afterwards, so edits you make on the disk survive a deploy.

## The worker's variables

Plain names, no bot prefix, the same set every bot uses on Render. `DATA` is the
mounted disk, not the Unix `HOME`. The non-secret ones come from `render.yaml`
and need no typing:

| Variable | Unset or blank | Accepted | What it does |
| --- | --- | --- | --- |
| `DATA` | `/data` | any path | the disk: `config.json`, `state/`, `logs/`, `reports/` |
| `PYTHONUNBUFFERED` | — | `1` | log lines appear as they happen |
| `BROKER` | the file's `broker` | `tasty`, `ibkr` | overrides `broker` in `/data/config.json`. The blueprint sets `tasty`. Any other value refuses to start |
| `MODE` | `live` | `live`, `dry`, `alerts` | `live` transmits; `dry` and `alerts` send nothing |
| `TASTY_DATA` | `production` | `production`, `sandbox` | the quotes, bars, chain, and greeks host |
| `TASTY_ORDERS` | `sandbox` | `sandbox`, `production` | the order host. `production` is real money |
| `BROKER_ACCOUNT` | the file's `broker_account` | any account id, or blank | blank is the first account on the order session |

`broker` in `/data/config.json` names the broker, and the seed ships
`"broker": "tasty"`. `BROKER` is this worker's override of that one key: the
blueprint sets `BROKER=tasty`, a blank or unset value leaves the file alone, and
an unknown value refuses to start. A disk seeded before `broker` was a file key
has no such key, and is never rewritten, so it runs on `BROKER=tasty` from the
blueprint; clearing `BROKER` there refuses with
`broker must be set in config.json: ibkr or tasty` until you add the key to
`/data/config.json`.

The list above is closed. The trading rules are code, not a config key. A
`plan` key left in `/data/config.json` from an older seed is ignored.

These six win over `/data/config.json`. The worker resolves them once at boot,
writes the result to `/data/state/service-config.json`, and points the session
and the wrap at that file, so what you set in the dashboard is what trades. Your
own edits to `/data/config.json` are still read first and are never overwritten.
The Mac launchers keep `SPX_NOON_HOME` and `SPX_NOON_CONFIG`. This worker does
not read them.

A push to `main` deploys itself, but only once the checks are green.
`autoDeployTrigger: checksPass` in `render.yaml` tells Render to wait for the
two GitHub checks on that commit, `test` and `docker`
(`.github/workflows/ci.yml`). Both pass and Render builds and deploys. Either
one fails and nothing deploys: the worker keeps running the commit it is on.
The trigger only takes effect once the Blueprint has been synced on Render;
until then the dashboard keeps the old manual setting.

A deploy still restarts the single worker, which interrupts the process, so time
the merge: no deploy between 11:45 and 15:10 ET, when a trade can be open. Time
the merge, not the deploy — the checks and then the image build run first, so a
merge at 11:40 can land the restart inside the window. Leave room, or wait until
after 15:10. That is advice to whoever merges, not a gate: neither Actions nor
Render refuses a deploy by the clock.

## What it does each day

| Time (ET) | What happens |
| --- | --- |
| overnight | `waiting` for the next session. Weekends, and the NYSE holidays and early closes on the hard-coded list, log `skip` and start no child. That list covers **2026 and 2027 only**, and a weekday after it is not a skipped year: the worker logs `waiting` that the day is outside the list, starts the session at 11:45 anyway, and the broker's own session hours decide. Regular hours that close at 16:00 or later may trade; tastytrade saying the session is closed, a close before 16:00, or no hours at all means no trade that day, and the 16:20 wrap runs either way. |
| 11:45 | starts the session as a child process: `python -m spx_noon` |
| 12:00–13:00 | the five 15-minute closes. At most one spread, long option nearest 80 delta, limit at the mid. The day is skipped when a 10:15–11:00 close already broke the prior day's high or low. |
| up to 15:00 | an open spread is managed to +8 SPX points, or flat two hours after the entry bar's close. Nothing is closed at 14:00. |
| 15:10 | with nothing open, the session stops looking and exits. |
| 16:20 | `python -m spx_noon wrap --now` once. Read-only: it places no order, and writes `reports/wrap-<date>.md`, then upserts one `High Noon` row on the shared sheet (below). |

If the session child crashes, the supervisor restarts it up to three times (5s,
15s, then 45s) and the restarted session reloads today's position from
`/data/state/own_position.json`. A clean exit is not restarted. After 15:10 a
crash is only restarted while today's position is still open or working. On a
deploy or a restart Render sends SIGTERM: the worker stops the child, releases
its lock, and exits; the new instance picks the position up off the same disk.

Nothing stops the session child inside trading hours. The one backstop is at
16:20, long after the last possible flat: a session still running then — the
runner stops trying after three failed sells and says so — is stopped and logged
as `error`, so tomorrow still happens. If you see that line, close the spread by
hand and check the alert.

## The 16:20 sheet row

The wrap writes `reports/wrap-<date>.md` first, then upserts **one** row for Boi
`High Noon` on the Google Sheet Candle and Fly already share, spreadsheet
`1tnMlDTidwCYG3_H2Z-lBcIUG5KOX4tgbe8AiRopATsU`. The columns are theirs and are
not changed here: `Date`, `Boi`, `Traded`, `P&L after fees`, `Worst`,
`What happened`. `Worst` stays empty — the trade log has no such column yet.
`P&L after fees` is the book's own `pnl`, which is already net of fees. The key
is `(Date, Boi)`, so a second wrap on the same date replaces that row instead of
adding one, and Candle's and Fly's rows are never read back or rewritten.

The row goes on the `Derek Comp` tab, the same one the Mac writes. There is no
tab setting.

A sheet problem is **quiet**: a missing key, a blob that is not service-account
json, the Google packages missing, or an API error each leave the markdown report
in place, append one status line to it, print that line, and exit 0. Render and
launchd see a successful wrap either way, so the status line is the record. Until
`SPX_SHEETS_CREDENTIALS_JSON` is pasted the log shows a line like
`not added to the sheet: no service-account key found …`. No status line ever
quotes the json, a `private_key`, or a key file's contents. The sheet must be
shared with the service account's `client_email`.

`spx_noon/sheetsink.py` imports `google.oauth2` and `googleapiclient` only on
the push itself; the image installs them with `".[sheets]"`.

The dashboard variable is the way in on Render. The same module would also read a
key file — `sheets_credentials` in the config, then `SPX_SHEETS_CREDENTIALS`,
then `GOOGLE_APPLICATION_CREDENTIALS`, then
`/data/state/sheets-service-account.json` — but a file on the disk means a
credential you have to put back by hand after a disk swap, and anything outside
`/data` is wiped by every deploy. Paste the json instead.

## Reading status

The service **Logs** tab in the Render dashboard is the whole story. There is no
URL to open and no health check to read. Lines are plain and start with one of
`boot`, `waiting`, `running`, `done`, `skip`, `restart`, or `error`:

```
boot broker=tasty mode=live home=/data config=/data/config.json child-config=/data/state/service-config.json DATA=api.tastyworks.com ORDERS=api.cert.tastyworks.com REAL MONEY=NO
waiting the 2026-11-16 session at 2026-11-16 11:45 ET, 3h12m away
running session 2026-11-16 try 1
```

`DATA=` and `ORDERS=` on that line are the two hosts, not the variables.
`REAL MONEY=YES` needs all three of `TASTY_DATA=production`,
`TASTY_ORDERS=production`, and `MODE=live`: a `dry` or `alerts` run transmits
nothing, so it says `NO` even with production orders. The environment itself is
never logged.

The session child's own lines (the safety banner, the decisions, the order log)
go to the same place. Account ids are shortened and secrets are redacted. The
same lines are on the disk in `/data/logs/` and `/data/state/bot_<date>.log`,
which you can also reach from a Render shell.

## Switching sandbox orders to real money

Both variables, then a redeploy. One alone is refused: `tasty_orders=production`
with sandbox data raises a `SafetyError` and the process will not start.

1. The worker → **Environment**.
2. `TASTY_ORDERS` → `production`.
3. `TASTY_DATA` is already `production`. Confirm it, do not remove it.
4. `MODE` is already `live`. Confirm it: `dry` and `alerts` send nothing.
5. Save, then **Manual Deploy**. Outside 11:45–15:10 ET.
6. Check the next `boot` line: `ORDERS=api.tastyworks.com` and `REAL MONEY=YES`.
   The orders now go to the production account in `BROKER_ACCOUNT` with the
   production credentials. Going back is the same trip with `sandbox`.

Sandbox orders need the sandbox credential pair; production orders need the
production pair. Keep both pasted so flipping back takes one variable.

## Another account

A second account is a second worker, not a code change. Nothing in the image or
the blueprint is specific to one account, and the variable names are the same for
every worker.

1. Render → **New → Blueprint** on the same repo, or **New → Background Worker**
   from the same Dockerfile, and give it its own name (`spx-noon-two`).
2. Its own disk, mounted at `/data` again. Disks are not shared, and two workers
   must never share one.
3. Its own secrets: that account's `BROKER_ACCOUNT`, its own credential
   pairs, and `SPX_SHEETS_CREDENTIALS_JSON` only if it should write the sheet.
   A worker only ever trades the account its own `BROKER_ACCOUNT` names.
   Two workers writing the same `(Date, High Noon)` row would overwrite each
   other, because the sheet has one row per bot per day and no account column.
4. The same non-secret variables, same values: `DATA=/data`,
   `PYTHONUNBUFFERED=1`, `BROKER=tasty`, `MODE=live`, `TASTY_DATA=production`,
   `TASTY_ORDERS=sandbox` until you switch that one worker to real money.

One rule: this worker must not share a tastytrade account with another job — a
second worker, or a local `tasty` config run by hand — on the same day. They do
not see each other's position file, so both would take the noon trade.

## Notes

- One disk means one instance. Do not raise instance count or add autoscaling,
  and do not add a web port to "make health checks work": a background worker
  has no health check.
- Everything outside `/data` is ephemeral and is wiped by every deploy.
- The Mac setup is unchanged. `run.sh`, `run_wrap.sh`, and `deploy/` have
  nothing to do with this worker.
