"""Noon breakout, plan v2. Version B is the default; A1 is the config key ``plan``.

Window: 15-minute bars that close at 12:00, 12:15, 12:30, 12:45, and 1:00 PM ET.
The first bar that qualifies is the candidate. At most one fill per day.

Calls are checked before puts. There is no moving-average gate.

A call bar qualifies when all three are true:
  * some earlier 15m close today, from the 9:45 close on, was above PDH
    (the first such bar is the trigger)
  * this close is still above PDH
  * this close is above the trigger bar's low

Puts mirror that with PDL and the trigger bar's high.

Both plans share that window, the trigger, the width, the mid limit, the $0.20
skip, and the two-hour flat. Plan ``A1`` changes exactly three things:

  * the day is skipped when a 15m close from 10:15 through 11:00 ET was already
    beyond PDH or PDL (:func:`morning_break`)
  * the long leg is the strike nearest 0.80 delta, not 0.75
  * the exit is +8 SPX points in the trade direction (:func:`point_exit_reached`)
    or the two-hour flat, whichever comes first. No premium target.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .pricing import (
    entry_skipped,
    limit_at_mid,
    quote_mid,
    slippage_per_side,
    spread_prices,
)

BAR_MINUTES = 15
TRIGGER_FROM = time(9, 45)
WINDOW_CLOSES = (time(12, 0), time(12, 15), time(12, 30), time(12, 45), time(13, 0))
TARGET_DELTA = 0.75
STRIKE_STEP = 5.0
RTH_OPEN = time(9, 30)
RTH_LAST_START = time(16, 0)

#: The two trading plans. ``B`` is the default and is what a missing key means.
#: The operator picks one in their own ``config.json``; nothing else selects it.
PLANS = ("B", "A1")
#: Plan A1's long leg. Plan B's is :data:`TARGET_DELTA`.
A1_TARGET_DELTA = 0.80
#: Plan A1's morning skip: these four closes only. A 9:45 close (bar start 9:30)
#: and an 11:15 close (bar start 11:00) are outside the window.
A1_MORNING_CLOSES = (time(10, 15), time(10, 30), time(10, 45), time(11, 0))
#: Plan A1's point exit, in SPX points from the entry print. Not a config key.
A1_EXIT_POINTS = 8.0


@dataclass
class Bar:
    t: datetime  # bar start, America/New_York
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class OptQuote:
    bid: float | None
    ask: float | None
    delta: float | None = None
    con_id: object | None = None
    iv: float | None = None
    volume: float | None = None
    time: datetime | None = None
    mdtype: int | None = None


@dataclass
class Px:
    value: float | None
    time: datetime | None = None
    mdtype: int | None = 1
    high: float | None = None


@dataclass
class Breakout:
    direction: str  # CALL or PUT
    right: str      # C or P
    entry: Bar
    trigger: Bar
    pdh: float
    pdl: float

    @property
    def entry_close(self) -> datetime:
        return close_time(self.entry)


@dataclass
class OrderPlan:
    direction: str
    right: str
    long_strike: float
    short_strike: float
    long_delta: float
    short_delta: float | None
    width: int
    em: float
    atm: float
    spot: float
    pdh: float
    pdl: float
    mid: float
    natural: float
    close_bid: float
    slippage_per_side: float
    limit: float
    skip: bool
    skip_reason: str
    long_con_id: object
    short_con_id: object
    entry_close: datetime
    trigger_close: datetime
    trigger_low: float
    trigger_high: float

    def legs(self) -> list[tuple]:
        return [(self.long_con_id, 1), (self.short_con_id, -1)]


def close_time(bar: Bar) -> datetime:
    return bar.t + timedelta(minutes=BAR_MINUTES)


def is_rth(bar: Bar) -> bool:
    start = bar.t.timetz().replace(tzinfo=None) if bar.t.tzinfo else bar.t.time()
    return RTH_OPEN <= start < RTH_LAST_START


def completed_bars(bars: list[Bar], now: datetime) -> list[Bar]:
    return [b for b in bars if close_time(b) <= now + timedelta(seconds=2)]


def session_bars(bars: list[Bar], day: date, now: datetime | None = None) -> list[Bar]:
    out = [b for b in bars if b.t.date() == day and is_rth(b)]
    if now is not None:
        out = [b for b in out if close_time(b) <= now + timedelta(seconds=2)]
    return sorted(out, key=lambda b: b.t)


def prior_session_day(bars: list[Bar], today: date) -> date | None:
    days = sorted({b.t.date() for b in bars if b.t.date() < today and is_rth(b)})
    return days[-1] if days else None


def pdh_pdl(bars: list[Bar], today: date) -> tuple[float, float, date] | None:
    """Prior day's RTH high and low: max high and min low of that session's 15m bars."""
    session = prior_session_day(bars, today)
    if session is None:
        return None
    day_bars = [b for b in bars if b.t.date() == session and is_rth(b)]
    if not day_bars:
        return None
    return max(b.h for b in day_bars), min(b.l for b in day_bars), session


def is_window_bar(bar: Bar) -> bool:
    return close_time(bar).time() in WINDOW_CLOSES


def decision_closes(day: date, tz) -> list[datetime]:
    return [datetime.combine(day, stamp, tz) for stamp in WINDOW_CLOSES]


def _prior_closes(today_bars: list[Bar], entry: Bar) -> list[Bar]:
    entry_close = close_time(entry)
    prior = [
        b for b in today_bars
        if b.t.date() == entry.t.date()
        and close_time(b) < entry_close
        and close_time(b).time() >= TRIGGER_FROM
        and is_rth(b)
    ]
    return sorted(prior, key=lambda b: b.t)


def qualifies(entry: Bar, today_bars: list[Bar], pdh: float, pdl: float) -> Breakout | None:
    """Return the breakout on this bar, or None.

    Calls first. The trigger is the first earlier close from 9:45 on beyond the level,
    not this bar and not a later retest.
    """
    if not is_window_bar(entry):
        return None
    prior = _prior_closes(today_bars, entry)
    call_triggers = [b for b in prior if b.c > pdh]
    if call_triggers and entry.c > pdh and entry.c > call_triggers[0].l:
        return Breakout("CALL", "C", entry, call_triggers[0], pdh, pdl)
    put_triggers = [b for b in prior if b.c < pdl]
    if put_triggers and entry.c < pdl and entry.c < put_triggers[0].h:
        return Breakout("PUT", "P", entry, put_triggers[0], pdh, pdl)
    return None


def first_breakout(today_bars: list[Bar], pdh: float, pdl: float,
                   up_to: datetime | None = None) -> Breakout | None:
    """First qualifying window bar whose close is at or before ``up_to``."""
    windows = [b for b in today_bars if is_window_bar(b)]
    windows.sort(key=lambda b: b.t)
    for bar in windows:
        if up_to is not None and close_time(bar) > up_to:
            break
        found = qualifies(bar, today_bars, pdh, pdl)
        if found is not None:
            return found
    return None


def plan_target_delta(plan: str = "B") -> float:
    """The long leg's delta for this plan: 0.80 on A1, 0.75 on B."""
    return A1_TARGET_DELTA if str(plan).strip().upper() == "A1" else TARGET_DELTA


def morning_break(today_bars: list[Bar], pdh: float, pdl: float) -> Bar | None:
    """Plan A1's morning skip. The first 10:15–11:00 close beyond PDH/PDL, or None.

    ``today_bars`` are today's RTH bars. Timestamps are bar starts, so the
    window is the bars starting 10:00, 10:15, 10:30, and 10:45. Beyond is
    strict: a close equal to PDH or PDL is not a break. Either side skips the
    whole day. A bar the broker did not return is not a break.
    """
    pdh, pdl = float(pdh), float(pdl)
    for bar in sorted((b for b in today_bars if is_rth(b)), key=lambda b: b.t):
        if close_time(bar).time() not in A1_MORNING_CLOSES:
            continue
        if bar.c > pdh or bar.c < pdl:
            return bar
    return None


def point_exit_reached(right: str, entry_print, live_print,
                       points: float = A1_EXIT_POINTS) -> bool:
    """Plan A1's point exit: is the live SPX print ``points`` through the entry print?

    ``entry_print`` is the live print at the entry decision, the ``spx`` stored
    on the position. Calls need ``live >= entry + points``, puts
    ``live <= entry − points``. Exactly ``points`` counts. A missing or
    non-finite print is not a hit.
    """
    try:
        entry, live = float(entry_print), float(live_print)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(entry) and math.isfinite(live)):
        return False
    distance = float(points)
    if right == "C":
        return live + 1e-9 >= entry + distance
    if right == "P":
        return live - 1e-9 <= entry - distance
    return False


def bar_closing_at(bars: list[Bar], when: datetime) -> Bar | None:
    for bar in bars:
        if abs((close_time(bar) - when).total_seconds()) < 1.0:
            return bar
    return None


def atm_strike(spot: float, step: float = STRIKE_STEP) -> float:
    """Spot rounded to the strike step. Halves round away from zero (6812.5 → 6815)."""
    return math.floor(float(spot) / step + 0.5) * step


def vertical_width(expected_move: float) -> int:
    """W = max(10, floor(EM / 5) × 5). EM is the ATM straddle mid, in points."""
    em = round(float(expected_move), 2)
    if not math.isfinite(em) or em <= 0:
        return 10
    stepped = math.floor(em / STRIKE_STEP + 1e-9) * int(STRIKE_STEP)
    return int(max(10, stepped))


def expected_move(quotes: dict, spot: float) -> tuple[float, float] | None:
    """ATM straddle mid. Strike is spot rounded to 5. Returns (atm, em) or None."""
    if spot is None or not math.isfinite(float(spot)) or float(spot) <= 0:
        return None
    book = _norm_quotes(quotes)
    atm = atm_strike(float(spot))
    call = quote_mid(book.get((atm, "C")))
    put = quote_mid(book.get((atm, "P")))
    if call is None or put is None:
        return None
    em = round(call + put, 2)
    if em <= 0:
        return None
    return atm, em


def select_long_strike(quotes: dict, right: str, spot: float,
                       target: float = TARGET_DELTA) -> tuple[float, float] | None:
    """Strike whose absolute delta is nearest ``target``, and in the money.

    Calls: strike below spot. Puts: strike above spot. Ties go to the strike
    closer to spot, then to the lower strike.
    """
    spot = float(spot)
    best: tuple | None = None
    for (strike, opt_right), quote in _norm_quotes(quotes).items():
        if opt_right != right:
            continue
        delta = getattr(quote, "delta", None)
        if delta is None:
            continue
        try:
            delta = float(delta)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(delta):
            continue
        if right == "C" and not strike < spot:
            continue
        if right == "P" and not strike > spot:
            continue
        dist = abs(abs(delta) - target)
        rank = (round(dist, 6), abs(strike - spot), strike)
        if best is None or rank < best[0]:
            best = (rank, strike, delta)
    if best is None:
        return None
    return best[1], best[2]


def short_strike(long_strike: float, width: float, right: str) -> float:
    if right == "C":
        return float(long_strike) + float(width)
    return float(long_strike) - float(width)


def lookup_quote(quotes: dict, strike: float, right: str):
    return _norm_quotes(quotes).get((round(float(strike), 2), right))


def _norm_quotes(quotes: dict) -> dict:
    out = {}
    for key, quote in quotes.items():
        strike, right = key
        out[(round(float(strike), 2), right)] = quote
    return out


def build_order_plan(breakout: Breakout, quotes: dict, spot: float,
                     plan: str = "B") -> tuple[OrderPlan | None, str]:
    """Turn a qualified bar into a 1-lot debit vertical, or a reason string.

    ``plan`` picks the long leg only: 0.75 delta on B, 0.80 on A1. The width,
    the short strike, the mid limit, and the $0.20 skip are the same on both.
    """
    if spot is None:
        return None, "no spot"
    try:
        spot_f = float(spot)
    except (TypeError, ValueError):
        return None, "no spot"
    if not math.isfinite(spot_f) or spot_f <= 0:
        return None, "no spot"
    em_pair = expected_move(quotes, spot_f)
    if em_pair is None:
        return None, "no atm straddle"
    atm, em = em_pair
    width = vertical_width(em)
    target = plan_target_delta(plan)
    picked = select_long_strike(quotes, breakout.right, spot_f, target)
    if picked is None:
        return None, f"no {round(target * 100)}-delta strike"
    long_strike, long_delta = picked
    short_k = short_strike(long_strike, width, breakout.right)
    book = _norm_quotes(quotes)
    long_q = book.get((round(long_strike, 2), breakout.right))
    short_q = book.get((round(short_k, 2), breakout.right))
    if long_q is None or short_q is None:
        return None, "quote missing for the long or short strike"
    if getattr(long_q, "con_id", None) is None or getattr(short_q, "con_id", None) is None:
        return None, "missing con id"
    prices = spread_prices(long_q, short_q)
    if prices is None:
        return None, "spread is not a positive debit"
    per_side = slippage_per_side(prices["natural"], prices["mid"])
    skip = entry_skipped(prices["natural"], prices["mid"])
    limit = limit_at_mid(prices["mid"])
    if limit <= 0:
        return None, "limit not positive"
    short_delta = getattr(short_q, "delta", None)
    plan = OrderPlan(
        direction=breakout.direction,
        right=breakout.right,
        long_strike=long_strike,
        short_strike=short_k,
        long_delta=long_delta,
        short_delta=None if short_delta is None else float(short_delta),
        width=width,
        em=em,
        atm=atm,
        spot=spot_f,
        pdh=breakout.pdh,
        pdl=breakout.pdl,
        mid=prices["mid"],
        natural=prices["natural"],
        close_bid=prices["close_bid"],
        slippage_per_side=per_side,
        limit=limit,
        skip=skip,
        skip_reason="slippage" if skip else "",
        long_con_id=long_q.con_id,
        short_con_id=short_q.con_id,
        entry_close=breakout.entry_close,
        trigger_close=close_time(breakout.trigger),
        trigger_low=breakout.trigger.l,
        trigger_high=breakout.trigger.h,
    )
    return plan, plan.skip_reason
