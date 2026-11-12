"""Read-only end-of-day note. It reads one trade book and writes a report.

``--book paper`` (the default) reads ``state/trades.csv`` and writes
``reports/wrap-<date>.md``. ``--book live`` reads ``state/trades-live.csv`` and
writes ``reports/wrap-live-<date>.md``. With no ``--book``, a validated IBKR
live config uses the live book and everything else uses paper.

It does not connect to a broker, it does not place, change, or cancel orders,
and ``--book live`` does not need the live latch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import SafetyError, load_config
from .logbook import Outputs

ET = ZoneInfo("America/New_York")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spx_noon wrap")
    parser.add_argument("--config", default=None)
    parser.add_argument("--book", choices=["paper", "live"], default=None,
                        help="paper reads state/trades.csv; live reads state/trades-live.csv")
    parser.add_argument("--now", action="store_true", help="Write immediately instead of waiting for 16:20 ET")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (SafetyError, ValueError, TypeError) as exc:
        print(f"refusing to wrap: {exc}")
        return 2
    cfg.ensure_dirs()
    book = args.book or ("live" if cfg.ibkr_live else "paper")
    now = datetime.now(ET)
    if not args.now and now.time() < time(16, 0):
        print(f"Wrap waits until 16:00 ET (now {now:%H:%M}). Pass --now to write the log summary anyway.")
        return 0
    out = Outputs(cfg.state_dir, book=book)
    rows = out.read_trades(now.date())
    label = "wrap live" if book == "live" else "wrap"
    lines = [
        f"# Noon {label} {now.date().isoformat()}",
        "",
        f"Source: state/{out.trade_path().name}. This job does not send orders.",
        "",
    ]
    if not rows:
        lines.append("No rows for today.")
    for row in rows:
        lines.append(
            f"- {row.get('direction') or ''} {row.get('long_strike') or ''}/{row.get('short_strike') or ''} "
            f"{row.get('exit_reason') or ''} fill {row.get('fill_price') or 'n/a'} "
            f"exit {row.get('exit_fill') or 'n/a'} pnl {row.get('pnl') or 'n/a'} "
            f"slip/side {row.get('slippage_per_side') or 'n/a'}"
        )
    lines.append("")
    name = f"wrap-live-{now.date().isoformat()}.md" if book == "live" else f"wrap-{now.date().isoformat()}.md"
    path = cfg.reports_dir / name
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path}")
    return 0
