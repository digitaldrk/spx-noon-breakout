"""Entry, width, delta, slippage, and exit rules."""
import pytest
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from spx_noon.pricing import (
    HARD_FLAT,
    entry_skipped,
    hard_flat_deadline,
    limit_at_mid,
    slippage_per_side,
    time_exit_deadline,
    trade_pnl,
)
from spx_noon.strategy import (
    EXIT_POINTS,
    TARGET_DELTA,
    Bar,
    OptQuote,
    atm_strike,
    build_order_plan,
    first_breakout,
    morning_break,
    pdh_pdl,
    point_exit_reached,
    qualifies,
    select_long_strike,
    short_strike,
    vertical_width,
    WINDOW_CLOSES,
)

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 28)
PREV = date(2026, 10, 27)
OLDER = date(2026, 10, 26)


def b(day, hh, mm, o, h, l, c):
    return Bar(datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET), o, h, l, c)


def levels():
    bars = [
        b(OLDER, 10, 0, 100, 8000, 50, 100),
        b(PREV, 9, 30, 6950, 7010, 6940, 7000),
        b(PREV, 15, 45, 6980, 7000, 6890, 6990),
    ]
    pdh, pdl, session = pdh_pdl(bars, DAY)
    assert session == PREV
    assert pdh == 7010
    assert pdl == 6890
    return bars, pdh, pdl


def today_call_bars():
    return [
        b(DAY, 9, 15, 7100, 7200, 6900, 7150),  # before 9:30, ignored
        b(DAY, 9, 30, 7005, 7020, 7002, 7012),  # 9:45 close, first trigger, low 7002
        b(DAY, 10, 0, 7002, 7009, 6998, 7006),  # 10:15 close, inside the levels
        b(DAY, 11, 30, 7018, 7040, 7016, 7030),  # 11:45 close, outside the window
        b(DAY, 11, 45, 7010, 7025, 7008, 7011),  # 12:00 close
        b(DAY, 12, 0, 7012, 7030, 7010, 7018),  # 12:15 close
    ]


def a1_call_bars():
    """The same day with the 10:00 bar dropped, so the morning window is quiet."""
    return [bar for bar in today_call_bars()
            if not (bar.t.hour == 10 and bar.t.minute == 0)]


def a1_fresh_call_bars():
    """A quiet morning whose 12:00 close is itself the day's first break of PDH.

    No close from 9:45 on is beyond PDH 7010 or PDL 6890 until the 12:00 close
    at 7011, and nothing in the 10:15–11:00 window is a break.
    """
    return [
        b(DAY, 9, 15, 7100, 7200, 6900, 7150),   # before 9:30, ignored on both plans
        b(DAY, 9, 30, 7000, 7008, 6995, 7002),   # 9:45 close, inside the levels
        b(DAY, 10, 0, 7002, 7009, 6998, 7005),   # 10:15 close, inside the levels
        b(DAY, 11, 30, 7005, 7010, 7000, 7008),  # 11:45 close, still inside
        b(DAY, 11, 45, 7008, 7025, 7006, 7011),  # 12:00 close, the first break of PDH
        b(DAY, 12, 0, 7012, 7020, 7002, 7005),   # 12:15 close, back inside PDH
    ]


def test_prior_day_high_and_low_ignore_older_sessions():
    _bars, pdh, pdl = levels()
    assert pdh == 7010 and pdl == 6890


def test_a_window_close_beyond_pdh_is_a_call_on_that_bar():
    bars, pdh, pdl = levels()
    today = today_call_bars()
    noon = today[-2]
    found = qualifies(noon, bars + today, pdh, pdl)
    assert found is not None
    assert found.direction == "CALL"
    assert found.entry is noon and found.trigger is noon


def test_eleven_forty_five_close_is_outside_the_window():
    bars, pdh, pdl = levels()
    today = today_call_bars()
    eleven = [bar for bar in today if bar.t.hour == 11 and bar.t.minute == 30][0]
    assert qualifies(eleven, bars + today, pdh, pdl) is None


def test_calls_win_when_both_sides_match():
    # Pathological levels so one close can be above PDH and below PDL.
    today = [b(DAY, 11, 45, 150, 160, 140, 150)]
    found = qualifies(today[-1], today, pdh=100, pdl=200)
    assert found is not None and found.direction == "CALL"


def test_width_rounds_the_expected_move_down_to_five_and_floors_at_ten():
    assert vertical_width(18.40) == 15
    assert vertical_width(9.90) == 10
    assert vertical_width(25) == 25
    assert vertical_width(10) == 10
    assert vertical_width(14.99) == 10
    assert vertical_width(15) == 15
    assert vertical_width(4) == 10


def test_atm_strike_rounds_half_away_from_zero():
    assert atm_strike(6812.4) == 6810
    assert atm_strike(6812.5) == 6815
    assert atm_strike(7000) == 7000


def test_long_strike_tie_goes_to_the_strike_closer_to_spot():
    spot = 7000
    quotes = {
        (6900, "C"): OptQuote(1, 1.1, delta=0.90, con_id=1),
        (6925, "C"): OptQuote(1, 1.1, delta=0.76, con_id=2),
        (6950, "C"): OptQuote(1, 1.1, delta=0.74, con_id=3),
        (7050, "C"): OptQuote(1, 1.1, delta=0.75, con_id=4),  # out of the money
        (7050, "P"): OptQuote(1, 1.1, delta=-0.74, con_id=5),
        (7075, "P"): OptQuote(1, 1.1, delta=-0.76, con_id=6),
        (6990, "P"): OptQuote(1, 1.1, delta=-0.80, con_id=7),  # call-side strike, not ITM as a put
    }
    # 0.76 and 0.74 are equally far from 0.75. The strike closer to spot wins.
    assert select_long_strike(quotes, "C", spot, 0.75) == (6950, 0.74)
    assert select_long_strike(quotes, "P", spot, 0.75) == (7050, -0.74)
    assert short_strike(6950, 30, "C") == 6980
    assert short_strike(7050, 15, "P") == 7035


def test_closer_delta_beats_the_tie_break():
    quotes = {
        (6925, "C"): OptQuote(1, 1.1, delta=0.751, con_id=1),
        (6950, "C"): OptQuote(1, 1.1, delta=0.74, con_id=2),
    }
    assert select_long_strike(quotes, "C", 7000)[0] == 6925


def test_slippage_boundary_is_strict():
    assert slippage_per_side(10.40, 10.00) == pytest.approx(0.20)
    assert entry_skipped(10.40, 10.00) is False
    assert entry_skipped(10.41, 10.00) is True
    assert entry_skipped(10.00, 10.00) is False
    assert entry_skipped(9.90, 10.00) is False
    assert limit_at_mid(12.13) == 12.10
    assert limit_at_mid(12.10) == 12.10


def test_order_plan_skips_a_wide_market_and_builds_the_tight_one():
    bars, pdh, pdl = levels()
    today = today_call_bars()
    bo = qualifies(today[-2], bars + today, pdh, pdl)
    tight = _chain(long_ask=40.2)
    plan, reason = build_order_plan(bo, tight, 7000)
    assert reason == ""
    assert plan is not None and plan.skip is False
    assert plan.direction == "CALL"
    assert plan.long_strike == 6950
    assert plan.width == 30
    assert plan.short_strike == 6980
    assert plan.em == 30
    assert plan.limit <= plan.mid
    wide = _chain(long_ask=44)
    skipped, why = build_order_plan(bo, wide, 7000)
    assert why == "slippage" and skipped.skip is True
    assert entry_skipped(skipped.natural, skipped.mid)


def _chain(long_ask: float):
    return {
        (7000, "C"): OptQuote(14.5, 15.5, delta=0.50, con_id=70001),
        (7000, "P"): OptQuote(14.5, 15.5, delta=-0.50, con_id=70002),
        (6800, "C"): OptQuote(80, 80.2, delta=0.95, con_id=68001),
        (6950, "C"): OptQuote(40.0, long_ask, delta=0.74, con_id=69501),
        (6980, "C"): OptQuote(28.0, 28.2, delta=0.40, con_id=69801),
    }


def test_the_flat_is_two_hours_after_the_bar_and_fees_come_off_pnl():
    noon = datetime(2026, 10, 28, 12, 0, tzinfo=ET)
    quarter = datetime(2026, 10, 28, 12, 15, tzinfo=ET)
    one = datetime(2026, 10, 28, 13, 0, tzinfo=ET)
    assert time_exit_deadline(noon) == datetime(2026, 10, 28, 14, 0, tzinfo=ET)
    assert time_exit_deadline(quarter) == datetime(2026, 10, 28, 14, 15, tzinfo=ET)
    assert time_exit_deadline(one) == datetime(2026, 10, 28, 15, 0, tzinfo=ET)
    pnl, fees = trade_pnl(12.71, 12.71 * 1.30)
    assert fees == 2.60
    assert pnl == pytest.approx((12.71 * 0.30 * 100) - 2.60)
    # The published 2026-07-31 winner was about +$378 on a $1,271 debit after fees.
    assert round(pnl) == 379


def test_the_hard_flat_is_1550_et_and_always_later_than_the_two_hour_clock():
    """The third exit is a backstop, so it never pulls the two-hour flat in."""
    assert HARD_FLAT == time(15, 50)
    for stamp in WINDOW_CLOSES:
        bar = datetime.combine(date(2026, 10, 28), stamp, ET)
        hard = hard_flat_deadline(bar)
        assert hard == datetime(2026, 10, 28, 15, 50, tzinfo=ET)
        assert hard > time_exit_deadline(bar)        # 13:00 is the latest: 15:00
    # the date alone works too, and the clock stays Eastern either way
    assert hard_flat_deadline(date(2026, 10, 28)) == datetime(2026, 10, 28, 15, 50, tzinfo=ET)
    # a UTC-stamped entry close is still the Eastern trading day's 15:50
    utc_noon = datetime(2026, 10, 28, 16, 0, tzinfo=ZoneInfo("UTC"))   # 12:00 ET
    assert hard_flat_deadline(utc_noon) == datetime(2026, 10, 28, 15, 50, tzinfo=ET)


def test_a_fresh_window_break_is_the_signal():
    bars, pdh, pdl = levels()                        # PDH 7010, PDL 6890
    today = a1_fresh_call_bars()
    noon = today[-2]                                 # the 12:00 close, 7011
    found = qualifies(noon, bars + today, pdh, pdl)
    assert found is not None and found.direction == "CALL"
    assert found.entry is noon and found.trigger is noon
    first = first_breakout(bars + today, pdh, pdl)
    assert first is not None and first.entry is noon
    assert morning_break(today, pdh, pdl) is None


def test_only_the_first_qualifying_window_close_is_taken():
    bars, pdh, pdl = levels()
    today = [
        b(DAY, 9, 30, 7000, 7008, 6995, 7002),       # 9:45 close, inside the levels
        b(DAY, 11, 45, 7008, 7025, 7006, 7011),      # 12:00 close, the first break
        b(DAY, 12, 0, 7012, 7030, 7010, 7018),       # 12:15 close, also beyond PDH
    ]
    first = first_breakout(bars + today, pdh, pdl)
    assert first is not None and first.entry is today[1]
    assert qualifies(today[2], bars + today, pdh, pdl) is not None


def test_a_break_after_noon_waits_for_its_own_close():
    bars, pdh, pdl = levels()
    today = [
        b(DAY, 11, 45, 7000, 7008, 6995, 7002),      # 12:00 close, inside the levels
        b(DAY, 12, 0, 7002, 7009, 6998, 7005),       # 12:15 close, still inside
        b(DAY, 12, 15, 7006, 7025, 7004, 7011),      # 12:30 close, the first break
    ]
    noon_fifteen = datetime(DAY.year, DAY.month, DAY.day, 12, 15, tzinfo=ET)
    half_past = datetime(DAY.year, DAY.month, DAY.day, 12, 30, tzinfo=ET)
    assert first_breakout(bars + today, pdh, pdl, noon_fifteen) is None
    found = first_breakout(bars + today, pdh, pdl, half_past)
    assert found is not None and found.entry is today[2]


def test_a_fresh_put_break_needs_no_earlier_close_below_pdl():
    bars, pdh, pdl = levels()
    today = [
        b(DAY, 9, 30, 7000, 7008, 6995, 7002),
        b(DAY, 11, 45, 6900, 6905, 6880, 6889),
    ]
    found = qualifies(today[-1], bars + today, pdh, pdl)
    assert found is not None and found.direction == "PUT"
    assert found.entry is today[-1] and found.trigger is today[-1]


def test_the_signal_is_strict_at_the_level_and_checks_calls_first():
    _bars, pdh, pdl = levels()
    equal_high = b(DAY, 11, 45, 7000, 7020, 6990, 7010)
    equal_low = b(DAY, 11, 45, 6900, 6910, 6880, 6890)
    assert qualifies(equal_high, [equal_high], pdh, pdl) is None
    assert qualifies(equal_low, [equal_low], pdh, pdl) is None
    eleven = b(DAY, 11, 30, 7005, 7030, 7000, 7011)
    assert qualifies(eleven, [eleven], pdh, pdl) is None
    both = b(DAY, 11, 45, 150, 160, 140, 150)
    found = qualifies(both, [both], pdh=100, pdl=200)
    assert found is not None and found.direction == "CALL"


def test_the_morning_skip_is_the_1015_through_1100_closes():
    _bars, pdh, pdl = levels()                       # PDH 7010, PDL 6890
    quiet = a1_call_bars()
    assert morning_break(quiet, pdh, pdl) is None
    # the 9:45 close (bar start 9:30) breaks PDH in that fixture and does not skip
    assert any(bar.t.hour == 9 and bar.c > pdh for bar in quiet)
    # the four closes in the window skip the day, either side of the levels
    for hour, minute in ((10, 0), (10, 15), (10, 30), (10, 45)):
        above = b(DAY, hour, minute, 7000, 7030, 7000, 7011)
        below = b(DAY, hour, minute, 7000, 7030, 6800, 6889)
        assert morning_break([above], pdh, pdl) is above
        assert morning_break([below], pdh, pdl) is below
    # 11:15 (bar start 11:00) is past the window, and 9:30 is before it
    assert morning_break([b(DAY, 11, 0, 7000, 7030, 7000, 7020)], pdh, pdl) is None
    assert morning_break([b(DAY, 9, 15, 7100, 7200, 6900, 7150)], pdh, pdl) is None
    # beyond is strict: equal to PDH or PDL is not a break
    assert morning_break([b(DAY, 10, 0, 7000, 7030, 6880, 7010)], pdh, pdl) is None
    assert morning_break([b(DAY, 10, 0, 7000, 7030, 6880, 6890)], pdh, pdl) is None
    # a bar inside the window that only traded through the level is not a break
    assert morning_break([b(DAY, 10, 0, 7000, 7050, 6800, 7005)], pdh, pdl) is None
    broke = morning_break(
        [b(DAY, 9, 30, 7000, 7012, 6990, 7005), b(DAY, 10, 0, 7000, 7030, 7000, 7011)],
        pdh, pdl)
    assert broke is not None and (broke.t.hour, broke.t.minute) == (10, 0)
    assert morning_break([], pdh, pdl) is None        # a missing bar is not a break
    assert morning_break(today_call_bars(), pdh, pdl) is None


def test_the_long_leg_is_the_in_the_money_delta_nearest_80():
    assert TARGET_DELTA == 0.80
    spot = 7000
    quotes = {
        (6900, "C"): OptQuote(1, 1.1, delta=0.90, con_id=1),
        (6925, "C"): OptQuote(1, 1.1, delta=0.80, con_id=2),
        (6950, "C"): OptQuote(1, 1.1, delta=0.74, con_id=3),
        (7050, "C"): OptQuote(1, 1.1, delta=0.80, con_id=4),   # out of the money
        (7050, "P"): OptQuote(1, 1.1, delta=-0.74, con_id=5),
        (7075, "P"): OptQuote(1, 1.1, delta=-0.81, con_id=6),  # sign is ignored
        (7100, "P"): OptQuote(1, 1.1, delta=-0.79, con_id=7),
    }
    assert select_long_strike(quotes, "C", spot) == (6925, 0.80)
    # 0.81 and 0.79 are equally far from 0.80; the strike closer to spot wins
    assert select_long_strike(quotes, "P", spot) == (7075, -0.81)


def test_the_order_buys_the_eighty_delta_and_names_a_missing_strike():
    bars, pdh, pdl = levels()
    today = today_call_bars()
    bo = qualifies(today[-2], bars + today, pdh, pdl)
    chain = _a1_chain()
    order, reason = build_order_plan(bo, chain, 7000)
    assert reason == "" and order.skip is False
    assert (order.long_strike, order.short_strike) == (6925, 6955)
    assert order.long_delta == 0.80
    assert order.width == 30 and order.em == 30 and order.limit <= order.mid
    assert order.slippage_per_side == pytest.approx(0.10)
    wide = _a1_chain(long_ask=58)
    skipped, why = build_order_plan(bo, wide, 7000)
    assert why == "slippage" and skipped.skip is True
    straddle_only = {
        (7000, "C"): OptQuote(14.5, 15.5, delta=0.50, con_id=70001),
        (7000, "P"): OptQuote(14.5, 15.5, delta=-0.50, con_id=70002),
    }
    assert build_order_plan(bo, straddle_only, 7000) == (None, "no 80-delta strike")


def _a1_chain(long_ask: float = 52.2):
    """B's chain plus the 80-delta strike and the short leg 30 points above it."""
    chain = _chain(long_ask=40.2)
    chain[(6925, "C")] = OptQuote(52.0, long_ask, delta=0.80, con_id=69251)
    chain[(6955, "C")] = OptQuote(34.0, 34.2, delta=0.60, con_id=69551)
    return chain


def test_the_point_exit_is_eight_points_in_the_trade_direction():
    assert EXIT_POINTS == 8.0
    assert point_exit_reached("C", 7000, 7008) is True       # exactly 8 counts
    assert point_exit_reached("C", 7000, 7020.5) is True
    assert point_exit_reached("C", 7000, 7007.99) is False
    assert point_exit_reached("C", 7000, 6980) is False      # against the trade
    assert point_exit_reached("P", 7000, 6992) is True
    assert point_exit_reached("P", 7000, 6980) is True
    assert point_exit_reached("P", 7000, 6992.01) is False
    assert point_exit_reached("P", 7000, 7020) is False
    # a missing or non-finite print is not a hit, on either leg
    for bad in (None, float("nan"), float("inf"), float("-inf"), "", "n/a"):
        assert point_exit_reached("C", 7000, bad) is False
        assert point_exit_reached("P", 7000, bad) is False
        assert point_exit_reached("C", bad, 7008) is False
