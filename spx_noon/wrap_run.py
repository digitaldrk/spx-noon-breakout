"""Read-only end-of-day note. It reads state/trades.csv and writes a report.

It does not connect to a broker and does not place, change, or cancel orders.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import load_config
from .logbook import Outputs

ET = ZoneInfo("America/New_York")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spx_noon wrap")
    parser.add_argument("--config", default=None)
    parser.add_argument("--now", action="store_true", help="Write immediately instead of waiting for 16:20 ET")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    cfg.ensure_dirs()
    now = datetime.now(ET)
    if not args.now and now.time() < time(16, 0):
        print(f"Wrap waits until 16:00 ET (now {now:%H:%M}). Pass --now to write the log summary anyway.")
        return 0
    out = Outputs(cfg.state_dir)
    rows = out.read_trades(now.date())
    lines = [
        f"# Noon wrap {now.date().isoformat()}",
        "",
        "Source: state/trades.csv. This job does not send orders.",
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
    path = cfg.reports_dir / f"wrap-{now.date().isoformat()}.md"
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path}")
    return 0
