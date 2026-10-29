"""Debit-spread prices for plan v2 version B.

One spread. Entry limit is the mid, rounded down to the nickel, so the order
never bids through the mid. The trade is skipped when the marketable debit is
more than $0.20 per side worse than that mid. Take-profit is debit × 1.30.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

TICK = 0.05
TP_MULTIPLE = 1.30
MAX_SLIPPAGE_PER_SIDE = 0.20
FEE_PER_CONTRACT = 0.65
N_LEGS = 2
HOLD_HOURS = 2
N_SIDES = 2


def floor_tick(price: float, tick: float = TICK) -> float:
    return round(math.floor(float(price) / tick + 1e-9) * tick, 2)


def limit_at_mid(mid: float, tick: float = TICK) -> float:
    """Buy limit at the mid, never above it."""
    return floor_tick(mid, tick)


def take_profit_limit(fill_debit: float, multiple: float = TP_MULTIPLE, tick: float = TICK) -> float:
    """Resting sell. Floored so a bid at debit × multiple can fill the order."""
    return floor_tick(float(fill_debit) * multiple, tick)


def target_reached(spread_bid: float | None, fill_debit: float, multiple: float = TP_MULTIPLE) -> bool:
    if spread_bid is None:
        return False
    try:
        bid = float(spread_bid)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(bid):
        return False
    return bid + 1e-9 >= float(fill_debit) * multiple


def time_exit_deadline(entry_bar_close: datetime) -> datetime:
    """Backtest clock: the 15m close of the entry bar, plus two hours.

    A 12:00 entry is flat at 2:00 PM. A 12:15 entry is flat at 2:15 PM.
    """
    return entry_bar_close + timedelta(hours=HOLD_HOURS)


def quote_mid(quote) -> float | None:
    if quote is None:
        return None
    bid, ask = getattr(quote, "bid", None), getattr(quote, "ask", None)
    if bid is None or ask is None:
        return None
    try:
        bid, ask = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return None
    if ask <= 0 or bid < 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def spread_prices(long_quote, short_quote) -> dict | None:
    """Prices of a long debit vertical, in points.

    mid: long mid − short mid
    natural: marketable debit (long ask − short bid)
    close_bid: credit available to sell the spread (long bid − short ask)
    """
    long_mid = quote_mid(long_quote)
    short_mid = quote_mid(short_quote)
    if long_mid is None or short_mid is None:
        return None
    mid = long_mid - short_mid
    natural = float(long_quote.ask) - float(short_quote.bid)
    close_bid = float(long_quote.bid) - float(short_quote.ask)
    if not math.isfinite(mid) or mid <= 0:
        return None
    if not math.isfinite(natural):
        return None
    return {
        "mid": mid,
        "natural": natural,
        "close_bid": close_bid,
        "long_mid": long_mid,
        "short_mid": short_mid,
    }


def slippage_per_side(worse_price: float, mid: float, n_sides: int = N_SIDES) -> float:
    """How far ``worse_price`` is from ``mid``, split across the two legs.

    For an entry, pass the marketable debit. Positive means more expensive than mid.
    """
    return (float(worse_price) - float(mid)) / n_sides


def entry_skipped(natural: float, mid: float, max_per_side: float = MAX_SLIPPAGE_PER_SIDE) -> bool:
    """Skip when a marketable fill would be more than $0.20 per side worse than mid.

    $0.20 per side exactly is still allowed. The backtest held up there and broke at $0.30.
    """
    return slippage_per_side(natural, mid) > max_per_side + 1e-9


def trade_pnl(entry_debit: float, exit_credit: float, qty: int = 1,
              fee: float = FEE_PER_CONTRACT) -> tuple[float, float]:
    """Dollar P&L of one vertical after $0.65 per contract per leg, open and close.

    Returns (pnl, fees). A 12.71 debit closed at 1.30× is about +$378.70.
    """
    fees = fee * qty * N_LEGS * 2
    gross = (float(exit_credit) - float(entry_debit)) * 100.0 * qty
    return gross - fees, fees
