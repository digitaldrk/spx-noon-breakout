"""Debit-spread prices for plan v2 version B.

One spread. The entry starts at the mid, rounded down to the nickel, so the
first order does not bid through that mid. The trade is skipped when the
marketable debit is more than $0.20 per side worse than that mid. Take-profit
is debit × 1.30.

IBKR paper may raise the working entry later. The raise is one nickel at a
time, at most ``PAPER_ENTRY_REPRICE_MAX_ADD`` ($0.10) above the original
floored mid, and never above the original marketable debit. That cap is fixed
from the prices that passed the entry check. A later quote cannot move it up.
$0.40 above the original mid is not an allowance this code will accept.
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
# Two nickels over the original floored mid. $0.05 a side, inside the band the
# plan calls a typical 0DTE fill, and under the $0.20/side skip. Not $0.40.
PAPER_ENTRY_REPRICE_MAX_ADD = 0.10
PAPER_ENTRY_REPRICE_SECS = 20.0
PAPER_ENTRY_REPRICE_ATTEMPTS = 6
REPRICE_MAX_ADD_CEILING = 0.15

# IBKR marketDataType. Anything else is not a usable live book.
_MD_CLASS = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}


def floor_tick(price: float, tick: float = TICK) -> float:
    return round(math.floor(float(price) / tick + 1e-9) * tick, 2)


def limit_at_mid(mid: float, tick: float = TICK) -> float:
    """Buy limit at the mid, never above it."""
    return floor_tick(mid, tick)


def require_combo_limit(limit: float, tick: float = TICK) -> float:
    """Combo orders use a $0.05 increment. Refuse a price that is not on that grid."""
    price = float(limit)
    floored = floor_tick(price, tick)
    if not math.isfinite(price) or floored <= 0:
        raise ValueError(f"non-positive limit {limit}")
    if abs(price - floored) > 1e-6:
        raise ValueError(f"off-tick limit {limit}")
    return floored


def entry_reprice_cap(original_limit: float, original_natural: float, max_add: float,
                      tick: float = TICK) -> float:
    """Highest later buy limit, fixed from the original validated prices.

    ``max_add`` is added to the original floored mid. The cap is also held to
    the original marketable debit, floored to the tick, so a later quote cannot
    raise it.
    """
    start = floor_tick(original_limit, tick)
    allowance = 0.0 if float(max_add) <= 0 else floor_tick(max_add, tick)
    raised = floor_tick(start + allowance, tick)
    natural_cap = floor_tick(original_natural, tick)
    if natural_cap + 1e-9 < start:
        natural_cap = start
    return min(raised, natural_cap)


def next_entry_limit(current: float, cap: float, mid: float | None, natural: float | None,
                     tick: float = TICK) -> float | None:
    """One tick toward the current mid. Never through the current mid or natural, never above ``cap``."""
    if mid is None or natural is None:
        return None
    try:
        current_f, cap_f, mid_f, natural_f = float(current), float(cap), float(mid), float(natural)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (current_f, cap_f, mid_f, natural_f)):
        return None
    if min(current_f, cap_f, mid_f, natural_f) <= 0:
        return None
    ceiling = min(cap_f, floor_tick(mid_f, tick), floor_tick(natural_f, tick))
    if ceiling <= current_f + 1e-9:
        return None
    target = min(floor_tick(current_f + tick, tick), ceiling)
    if target <= current_f + 1e-9 or target > cap_f + 1e-9:
        return None
    return target


def market_data_class(mdtype) -> str:
    """IBKR market-data class. Missing and unknown values are not treated as live."""
    if mdtype is None:
        return "missing"
    try:
        code = int(mdtype)
    except (TypeError, ValueError):
        return "invalid"
    return _MD_CLASS.get(code, "invalid")


def quote_price_problem(quote) -> str | None:
    """``missing`` or ``invalid`` when the bid/ask cannot price a spread. None when the pair is usable."""
    if quote is None:
        return "missing"
    bid, ask = getattr(quote, "bid", None), getattr(quote, "ask", None)
    if bid is None or ask is None:
        return "missing"
    try:
        bid_f, ask_f = float(bid), float(ask)
    except (TypeError, ValueError):
        return "invalid"
    if not (math.isfinite(bid_f) and math.isfinite(ask_f)):
        return "invalid"
    if ask_f <= 0 or bid_f < 0 or ask_f < bid_f:
        return "invalid"
    return None


def reprice_quote_block(long_quote, short_quote, now: datetime, max_age_s: float) -> str | None:
    """None when both legs may support a raise. Otherwise a short reason.

    ``quote.time`` on the IBKR path is the local clock when ib_async last applied
    a tick batch to that ticker. It is not the exchange bid/ask time. A recent
    stamp does not prove the bid and ask are fresh. A missing or old stamp means
    no tick has been applied, so the raise is refused. Live data still requires
    market-data class ``live`` (IBKR type 1). Frozen, delayed, and delayed-frozen
    books are refused.
    """
    for name, quote in (("long", long_quote), ("short", short_quote)):
        problem = quote_price_problem(quote)
        if problem:
            return f"{name} {problem}"
        kind = market_data_class(getattr(quote, "mdtype", None))
        if kind != "live":
            return f"{name} {kind}"
        stamp = getattr(quote, "time", None)
        if stamp is None or getattr(stamp, "tzinfo", None) is None:
            return f"{name} no receive stamp"
        age = (now - stamp).total_seconds()
        if age < -5:
            return f"{name} receive stamp in the future"
        if age > max_age_s:
            return f"{name} stream quiet {age:.0f}s"
    if spread_prices(long_quote, short_quote) is None:
        return "spread not a positive debit"
    return None


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
