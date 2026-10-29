"""Read-only Schwab check. previewOrder is validation only and only with --preview-sample."""
from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import load_config
from .schwab import SchwabBroker, build_vertical_order, option_symbol

ET = ZoneInfo("America/New_York")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spx_noon check")
    parser.add_argument("--preview-sample", action="store_true",
                        help="Run previewOrder on a 1-lot vertical. Does not place it.")
    args = parser.parse_args(argv)
    cfg = load_config()
    if cfg.broker != "schwab":
        cfg = load_config(broker="schwab")
    broker = SchwabBroker(cfg)
    broker.connect()
    accounts = broker.managed_accounts()
    print(f"Accounts: {accounts}")
    px = broker.spx_px()
    print(f"SPX {px.value} at {datetime.now(ET):%H:%M:%S} ET")
    today = datetime.now(ET).strftime("%Y%m%d")
    strikes = broker.chain_strikes(today)
    print(f"SPXW strikes today: {0 if not strikes else len(strikes)}")
    if args.preview_sample and px.value and strikes:
        spot = float(px.value)
        atm = min(strikes, key=lambda strike: abs(strike - spot))
        short = atm + 10
        if short not in strikes:
            short = min((strike for strike in strikes if strike > atm), default=None)
        if short is not None:
            order = build_vertical_order(
                [(option_symbol(today, atm, "C"), 1), (option_symbol(today, short, "C"), -1)],
                "BUY", 1, 1.00, tif="DAY")
            preview = broker.c.preview_order(order)
            print(f"previewOrder ok={preview.get('ok')} rejects={preview.get('rejects')}")
            print("Preview only. Nothing was placed.")
    else:
        print("Check only. No orders.")
    return 0
