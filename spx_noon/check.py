"""Read-only smoke test. Places no orders.

Mac: TWS paper on 127.0.0.1:7497. Windows: Schwab quotes and the SPXW chain.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import PAPER_PORT, load_config

ET = ZoneInfo("America/New_York")


def main(argv=None) -> int:
    if sys.platform == "win32":
        from .check_schwab import main as schwab_main
        return schwab_main(argv)
    if sys.platform != "darwin":
        print("Noon Breakout check runs on Mac (IBKR paper) or Windows (Schwab)")
        return 2
    return ib_main(argv)


def ib_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spx_noon check")
    parser.add_argument("--client-id", type=int, default=None)
    args = parser.parse_args(argv)
    cfg = load_config(broker="ibkr")
    client_id = args.client_id or cfg.check_client_id
    if int(cfg.port) != PAPER_PORT:
        print("refusing: not the paper port")
        return 2
    from ib_async import IB, Index

    ib = IB()
    print(f"Connecting read-only to TWS paper at 127.0.0.1:{PAPER_PORT} (clientId {client_id}) ...")
    ib.connect("127.0.0.1", PAPER_PORT, clientId=client_id, timeout=15, readonly=True)
    try:
        accounts = ib.managedAccounts()
        paper = bool(accounts) and all(str(acct).startswith("D") for acct in accounts)
        print(f"Accounts: {accounts} -> {'PAPER OK' if paper else 'NOT PAPER — the bot will refuse to send'}")
        ib.reqMarketDataType(1)
        spx = Index("SPX", "CBOE", "USD")
        ib.qualifyContracts(spx)
        ticker = ib.reqMktData(spx, "", False, False)
        ib.sleep(2)
        print(f"SPX last {ticker.last}  bid {ticker.bid}  ask {ticker.ask}  at {datetime.now(ET):%H:%M:%S} ET")
        chains = ib.reqSecDefOptParams("SPX", "", "IND", spx.conId)
        today = datetime.now(ET).strftime("%Y%m%d")
        expirations = []
        for chain in chains:
            if chain.tradingClass == "SPXW" and chain.exchange == "SMART":
                expirations = sorted(chain.expirations)
                print(f"SPXW expirations include today: {today in chain.expirations}  ({len(chain.strikes)} strikes)")
        if today not in expirations:
            print("No SPXW expiration for today.")
        print("Check only. No orders.")
        return 0 if paper else 1
    finally:
        ib.disconnect()
