"""Read-only smoke test. Places no orders.

Mac auto: TWS paper on 127.0.0.1:7497.
Windows auto: tastytrade (quotes, bars, chain, orders host).
``--broker schwab`` still runs the Schwab check.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import PAPER_PORT, SafetyError, load_config, resolve_broker

ET = ZoneInfo("America/New_York")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spx_noon check")
    parser.add_argument("--broker", default="auto",
                        help="auto, tasty, tt, tastytrade, ibkr, or schwab")
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument("--preview-sample", action="store_true",
                        help="Schwab only: previewOrder on a 1-lot vertical. Does not place it.")
    args = parser.parse_args(argv)
    try:
        broker = resolve_broker(args.broker)
    except SafetyError as exc:
        print(f"refusing: {exc}")
        return 2
    if args.broker == "auto" and sys.platform not in ("darwin", "win32"):
        print("Noon Breakout check runs on Mac (IBKR paper) or Windows (tastytrade). Pass --broker to override.")
        return 2
    if broker == "tasty":
        from .check_tasty import main as tasty_main
        return tasty_main(["--broker", "tasty"])
    if broker == "schwab":
        from .check_schwab import main as schwab_main
        forwarded = ["--preview-sample"] if args.preview_sample else []
        return schwab_main(forwarded)
    if broker == "ibkr":
        return ib_main(args.client_id)
    print(f"refusing: check does not run for {broker}")
    return 2


def ib_main(client_id_arg=None) -> int:
    cfg = load_config(broker="ibkr")
    client_id = client_id_arg or cfg.check_client_id
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
