"""Read-only tastytrade check. Places no orders.

Reports the safety banner, accounts on the order session, an SPX quote,
15-minute and 1-minute bars, the SPXW chain, and VIX. All of that is tastytrade.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import SafetyError, load_config, resolve_broker
from .tasty import TastyBroker

ET = ZoneInfo("America/New_York")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spx_noon check --broker tasty")
    parser.add_argument("--broker", default="tasty")
    args = parser.parse_args(argv)
    try:
        broker_name = resolve_broker(args.broker)
        if broker_name != "tasty":
            raise SafetyError(f"check_tasty got broker {broker_name}")
        cfg = load_config(broker="tasty")
        broker = TastyBroker(cfg)
        broker.connect()
    except (SafetyError, ValueError, TypeError) as exc:
        print(f"refusing: {exc}")
        return 2
    try:
        accounts = broker.managed_accounts()
        print(f"Accounts: {accounts}")
        px = broker.spx_px()
        print(f"SPX {px.value} at {datetime.now(ET):%H:%M:%S} ET")
        bars_15 = broker.spx_bars_15m()
        bars_1 = broker.spx_bars_1m(days=1)
        print(f"15m bars: {len(bars_15)}  1m bars: {len(bars_1)}")
        today = datetime.now(ET).strftime("%Y%m%d")
        strikes = broker.chain_strikes(today)
        print(f"SPXW strikes today: {0 if not strikes else len(strikes)}")
        vix = broker.vix_px()
        print(f"VIX {vix.value}")
        print("Check only. No orders.")
        ok = px.value is not None and strikes and vix.value is not None and bars_15
        return 0 if ok else 1
    finally:
        broker.disconnect()
