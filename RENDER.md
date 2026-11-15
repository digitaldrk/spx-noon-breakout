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
3. Render will ask for the five secret values before the first deploy finishes.
   Paste them in the dashboard (**the worker → Environment**). They are never in
   the repo, the image, or a log line:

   | Variable | What it is |
   | --- | --- |
   | `TASTY_ACCOUNT` | tastytrade account to trade. Blank only works when the order session has exactly one account. |
   | `TASTYTRADE_PROD_CLIENT_SECRET` | production OAuth client secret (quotes) |
   | `TASTYTRADE_PROD_REFRESH_TOKEN` | production refresh token (quotes) |
   | `TASTYTRADE_SANDBOX_CLIENT_SECRET` | sandbox OAuth client secret (orders) |
   | `TASTYTRADE_SANDBOX_REFRESH_TOKEN` | sandbox refresh token (orders) |

   Production credentials are needed even in the default setup: quotes, bars,
   the chain, and greeks come from the production host and never fall back to
   sandbox data.
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
| `BROKER` | refuses to start | `tasty` | required. The blueprint sets `tasty`. Anything else, `ibkr` included, refuses to start |
| `MODE` | `live` | `live`, `dry`, `alerts` | `live` transmits; `dry` and `alerts` send nothing |
| `TASTY_DATA` | `production` | `production`, `sandbox` | the quotes, bars, chain, and greeks host |
| `TASTY_ORDERS` | `sandbox` | `sandbox`, `production` | the order host. `production` is real money |
| `TASTY_ACCOUNT` | the file's account | any account id, or blank | blank is the first account on the order session |

`BROKER` is required, and `tasty` is the one spelling it accepts. The blueprint
sets `BROKER=tasty`. A missing value refuses to start, and so does any other
value, `ibkr` included.

These six win over `/data/config.json`. The worker resolves them once at boot,
writes the result to `/data/state/service-config.json`, and points the session
and the wrap at that file, so what you set in the dashboard is what trades. Your
own edits to `/data/config.json` are still read first and are never overwritten.
The Mac and Windows launchers keep `SPX_NOON_HOME` and `SPX_NOON_CONFIG`. This
worker does not read them.

`autoDeploy` is off. Deploys interrupt the process, so you choose when: **Manual
Deploy → Deploy latest commit**. Never between 11:45 and 15:10 ET, when a trade
can be open.

## What it does each day

| Time (ET) | What happens |
| --- | --- |
| overnight | `waiting` for the next session. Weekends, NYSE holidays, early closes, and any year past the hard-coded 2026–2027 list log `skip` and trade nothing. |
| 11:45 | starts the session as a child process: `python -m spx_noon` |
| 12:00–13:00 | the five 15-minute closes. At most one spread, long option nearest 75 delta, limit at the mid. |
| up to 15:00 | an open spread is managed to its +30% target or flat two hours after the entry bar. Nothing is closed at 14:00. |
| 15:10 | with nothing open, the session stops looking and exits. |
| 16:20 | `python -m spx_noon wrap --now` once. Read-only: it connects to nothing and sends nothing, and writes `reports/wrap-<date>.md`. |

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
   The orders now go to the production account in `TASTY_ACCOUNT` with the
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
3. Its own five secrets: that account's `TASTY_ACCOUNT` and its own credential
   pairs. A worker only ever trades the account its own `TASTY_ACCOUNT` names.
4. The same non-secret variables, same values: `DATA=/data`,
   `PYTHONUNBUFFERED=1`, `BROKER=tasty`, `MODE=live`, `TASTY_DATA=production`,
   `TASTY_ORDERS=sandbox` until you switch that one worker to real money.

One rule: do not run this worker and the Windows or Mac job against the same
tastytrade account on the same day. They do not see each other's position file,
so both would take the noon trade.

## Notes

- One disk means one instance. Do not raise instance count or add autoscaling,
  and do not add a web port to "make health checks work": a background worker
  has no health check.
- Everything outside `/data` is ephemeral and is wiped by every deploy.
- The Mac and Windows setups are unchanged. `run.sh`, `run_wrap.sh`,
  `run_session.ps1`, and `deploy/` have nothing to do with this worker.
