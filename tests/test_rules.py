"""Entry, width, delta, slippage, and exit rules from plan v2: version B and plan A1."""
import pytest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from spx_noon.pricing import (
    entry_skipped,
    limit_at_mid,
    slippage_per_side,
    take_profit_limit,
    target_reached,
    time_exit_deadline,
    trade_pnl,
)
from spx_noon.strategy import (
    A1_EXIT_POINTS,
    A1_TARGET_DELTA,
    Bar,
    OptQuote,
    atm_strike,
    build_order_plan,
    first_breakout,
    morning_break,
    pdh_pdl,
    plan_target_delta,
    point_exit_reached,
    qualifies,
    select_long_strike,
    short_strike,
    vertical_width,
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
        b(DAY, 10, 0, 7014, 7030, 7008, 7022),  # later break, higher low
        b(DAY, 11, 30, 7018, 7040, 7016, 7030),  # 11:45 close, outside the window
        b(DAY, 11, 45, 7010, 7025, 7008, 7011),  # 12:00 close
        b(DAY, 12, 0, 7012, 7030, 7010, 7018),  # 12:15 close
    ]


def a1_call_bars():
    """The same day with nothing in A1's 10:15–11:00 window.

    Only the 10:00 bar is dropped, so the 9:45 close is still the trigger: a
    9:45 break is a valid A1 entry and does not skip the day.
    """
    return [bar for bar in today_call_bars()
            if not (bar.t.hour == 10 and bar.t.minute == 0)]


def a1_fresh_call_bars():
    """A quiet morning whose 12:00 close is itself the day's first break of PDH.

    No close from 9:45 on, and no later window close, is beyond PDH 7010 or
    PDL 6890, so version B never has a trigger and never enters on this day.
    A1 takes the 12:00 close at 7011.
    """
    return [
        b(DAY, 9, 15, 7100, 7200, 6900, 7150),   # before 9:30, ignored on both plans
        b(DAY, 9, 30, 7000, 7008, 6995, 7002),   # 9:45 close, inside the levels
        b(DAY, 10, 0, 7002, 7009, 6998, 7005),   # 10:15 close, no A1 morning break
        b(DAY, 11, 30, 7005, 7010, 7000, 7008),  # 11:45 close, still inside
        b(DAY, 11, 45, 7008, 7025, 7006, 7011),  # 12:00 close, the first break of PDH
        b(DAY, 12, 0, 7012, 7020, 7002, 7005),   # 12:15 close, back inside PDH
    ]


def test_prior_day_high_and_low_ignore_older_sessions():
    _bars, pdh, pdl = levels()
    assert pdh == 7010 and pdl == 6890


def test_call_uses_the_first_trigger_low():
    bars, pdh, pdl = levels()
    today = today_call_bars()
    # 7011 is above PDH and above the first trigger low (7002), but below the later bar's low (7008).
    found = qualifies(today[-2], bars + today, pdh, pdl)
    assert found is not None
    assert found.direction == "CALL"
    assert found.trigger.t.hour == 9 and found.trigger.t.minute == 30
    assert found.entry.c < found.trigger.c  # still qualifies with no average gate


def test_eleven_forty_five_close_is_outside_the_window():
    bars, pdh, pdl = levels()
    today = today_call_bars()
    eleven = [bar for bar in today if bar.t.hour == 11 and bar.t.minute == 30][0]
    assert qualifies(eleven, bars + today, pdh, pdl) is None


def test_pre_open_break_does_not_count_and_nine_forty_five_does():
    bars, pdh, pdl = levels()
    early_only = [
        b(DAY, 9, 15, 7100, 7200, 6900, 7150),
        b(DAY, 11, 45, 7010, 7025, 7008, 7015),
    ]
    assert qualifies(early_only[-1], bars + early_only, pdh, pdl) is None
    with_open = early_only + [b(DAY, 9, 30, 7005, 7020, 7002, 7012)]
    # 9:30 bar is earlier than the noon bar even though it was appended last
    found = qualifies(early_only[-1], bars + with_open, pdh, pdl)
    assert found is not None and found.trigger.t.hour == 9


def test_the_entry_bar_cannot_be_its_own_trigger():
    bars, pdh, pdl = levels()
    noon = b(DAY, 11, 45, 7000, 7020, 7004, 7015)  # first close above PDH
    later = b(DAY, 12, 0, 7012, 7022, 7006, 7016)
    assert qualifies(noon, bars + [noon, later], pdh, pdl) is None
    found = qualifies(later, bars + [noon, later], pdh, pdl)
    assert found is not None
    assert found.trigger is noon


def test_put_uses_the_first_trigger_high():
    bars, pdh, pdl = levels()
    first = b(DAY, 10, 0, 6900, 6950, 6870, 6880)  # close below PDL, high 6950
    later = b(DAY, 11, 0, 6880, 6880, 6860, 6870)  # high 6880
    noon = b(DAY, 11, 45, 6884, 6889, 6880, 6888)  # below PDL and below 6950, not below 6880
    found = qualifies(noon, bars + [first, later, noon], pdh, pdl)
    assert found is not None
    assert found.direction == "PUT"
    assert found.trigger is first


def test_calls_win_when_both_sides_match():
    # Pathological levels so one close can be above PDH and below PDL.
    today = [
        b(DAY, 10, 0, 150, 160, 140, 155),
        b(DAY, 11, 45, 150, 160, 140, 150),
    ]
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


def test_long_strike_is_the_in_the_money_delta_nearest_75():
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
    assert select_long_strike(quotes, "C", spot) == (6950, 0.74)
    assert select_long_strike(quotes, "P", spot) == (7050, -0.74)
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


def test_target_is_thirty_percent_and_the_flat_is_two_hours_after_the_bar():
    noon = datetime(2026, 10, 28, 12, 0, tzinfo=ET)
    quarter = datetime(2026, 10, 28, 12, 15, tzinfo=ET)
    one = datetime(2026, 10, 28, 13, 0, tzinfo=ET)
    assert time_exit_deadline(noon) == datetime(2026, 10, 28, 14, 0, tzinfo=ET)
    assert time_exit_deadline(quarter) == datetime(2026, 10, 28, 14, 15, tzinfo=ET)
    assert time_exit_deadline(one) == datetime(2026, 10, 28, 15, 0, tzinfo=ET)
    assert take_profit_limit(10) == 13.0
    assert take_profit_limit(12.71) == 16.50
    assert target_reached(13.0, 10.0)
    assert not target_reached(12.99, 10.0)
    assert not target_reached(None, 10.0)
    pnl, fees = trade_pnl(12.71, 12.71 * 1.30)
    assert fees == 2.60
    assert pnl == pytest.approx((12.71 * 0.30 * 100) - 2.60)
    # The published 2026-07-31 winner was about +$378 on a $1,271 debit after fees.
    assert round(pnl) == 379


# --------------------------------------------------------------------------
# Plan A1: the fresh break, the morning skip, the 80-delta long leg, and the
# +8 point exit
# --------------------------------------------------------------------------

def test_a1_takes_a_fresh_window_break_that_version_b_passes_on():
    bars, pdh, pdl = levels()                        # PDH 7010, PDL 6890
    today = a1_fresh_call_bars()
    noon = today[-2]                                 # the 12:00 close, 7011
    # version B has no trigger on this day, with or without the plan argument
    assert qualifies(noon, bars + today, pdh, pdl) is None
    assert qualifies(noon, bars + today, pdh, pdl, "B") is None
    found = qualifies(noon, bars + today, pdh, pdl, "A1")
    assert found is not None and found.direction == "CALL"
    assert found.entry is noon
    assert found.trigger is noon                     # the break bar, not a trigger rule
    # the scanner agrees with the qualifier on both plans
    first = first_breakout(bars + today, pdh, pdl, plan="A1")
    assert first is not None and first.entry is noon
    assert first_breakout(bars + today, pdh, pdl) is None
    # A1's morning skip is untouched: nothing from 10:15 to 11:00 is beyond the levels
    assert morning_break(today, pdh, pdl) is None


def test_a1_still_takes_only_the_first_qualifying_window_close():
    bars, pdh, pdl = levels()
    today = [
        b(DAY, 9, 30, 7000, 7008, 6995, 7002),       # 9:45 close, inside the levels
        b(DAY, 11, 45, 7008, 7025, 7006, 7011),      # 12:00 close, the first break
        b(DAY, 12, 0, 7012, 7030, 7010, 7018),       # 12:15 close, also beyond PDH
    ]
    first = first_breakout(bars + today, pdh, pdl, plan="A1")
    assert first is not None and first.entry is today[1]
    # the later bar on its own would still qualify; it is simply not the first
    assert qualifies(today[2], bars + today, pdh, pdl, "A1") is not None


def test_an_a1_break_after_noon_waits_for_its_own_close():
    bars, pdh, pdl = levels()
    today = [
        b(DAY, 11, 45, 7000, 7008, 6995, 7002),      # 12:00 close, inside the levels
        b(DAY, 12, 0, 7002, 7009, 6998, 7005),       # 12:15 close, still inside
        b(DAY, 12, 15, 7006, 7025, 7004, 7011),      # 12:30 close, the first break
    ]
    noon_fifteen = datetime(DAY.year, DAY.month, DAY.day, 12, 15, tzinfo=ET)
    half_past = datetime(DAY.year, DAY.month, DAY.day, 12, 30, tzinfo=ET)
    assert first_breakout(bars + today, pdh, pdl, noon_fifteen, "A1") is None
    found = first_breakout(bars + today, pdh, pdl, half_past, "A1")
    assert found is not None and found.entry is today[2]


def test_a1_takes_a_fresh_put_break_with_no_earlier_close_below_pdl():
    bars, pdh, pdl = levels()
    today = [
        b(DAY, 9, 30, 7000, 7008, 6995, 7002),       # 9:45 close, inside the levels
        b(DAY, 11, 45, 6900, 6905, 6880, 6889),      # 12:00 close, the first break of PDL
    ]
    assert qualifies(today[-1], bars + today, pdh, pdl) is None
    found = qualifies(today[-1], bars + today, pdh, pdl, "A1")
    assert found is not None and found.direction == "PUT"
    assert found.entry is today[-1] and found.trigger is today[-1]


def test_the_a1_signal_is_strict_at_the_level_and_still_checks_calls_first():
    _bars, pdh, pdl = levels()
    # equal to PDH or PDL is not beyond it
    equal_high = b(DAY, 11, 45, 7000, 7020, 6990, 7010)
    equal_low = b(DAY, 11, 45, 6900, 6910, 6880, 6890)
    assert qualifies(equal_high, [equal_high], pdh, pdl, "A1") is None
    assert qualifies(equal_low, [equal_low], pdh, pdl, "A1") is None
    # the window is the same five closes: an 11:45 close is outside it on A1 too
    eleven = b(DAY, 11, 30, 7005, 7030, 7000, 7011)
    assert qualifies(eleven, [eleven], pdh, pdl, "A1") is None
    # pathological levels so one close is both above PDH and below PDL: calls win
    both = b(DAY, 11, 45, 150, 160, 140, 150)
    found = qualifies(both, [both], pdh=100, pdl=200, plan="A1")
    assert found is not None and found.direction == "CALL"
    # whitespace and case on the plan name are forgiven; anything else is version B
    noon = b(DAY, 11, 45, 7008, 7025, 7006, 7011)
    assert qualifies(noon, [noon], pdh, pdl, " a1 ") is not None
    assert qualifies(noon, [noon], pdh, pdl, "B") is None
    assert qualifies(noon, [noon], pdh, pdl, "") is None

def test_the_a1_morning_skip_is_the_1015_through_1100_closes():
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
    # one such close is the whole day, and the first one is the one reported
    broke = morning_break(today_call_bars(), pdh, pdl)
    assert broke is not None and (broke.t.hour, broke.t.minute) == (10, 0)
    assert morning_break([], pdh, pdl) is None        # a missing bar is not a break


def test_the_a1_long_leg_is_the_in_the_money_delta_nearest_80():
    assert A1_TARGET_DELTA == 0.80
    assert plan_target_delta("A1") == 0.80
    assert plan_target_delta("B") == plan_target_delta() == 0.75
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
    assert select_long_strike(quotes, "C", spot, A1_TARGET_DELTA) == (6925, 0.80)
    assert select_long_strike(quotes, "C", spot) == (6950, 0.74)
    # 0.81 and 0.79 are equally far from 0.80; the strike closer to spot wins
    assert select_long_strike(quotes, "P", spot, A1_TARGET_DELTA) == (7075, -0.81)


def test_b_and_a1_price_the_same_chain_with_their_own_long_leg():
    bars, pdh, pdl = levels()
    today = today_call_bars()
    bo = qualifies(today[-2], bars + today, pdh, pdl)
    chain = _a1_chain()
    version_b, reason = build_order_plan(bo, chain, 7000)
    assert reason == "" and version_b.skip is False
    assert (version_b.long_strike, version_b.short_strike) == (6950, 6980)
    a1, reason = build_order_plan(bo, chain, 7000, "A1")
    assert reason == "" and a1.skip is False
    assert (a1.long_strike, a1.short_strike) == (6925, 6955)
    assert a1.long_delta == 0.80
    # width, the mid limit, and the $0.20 skip are the same rule on both plans
    assert a1.width == version_b.width == 30
    assert a1.em == version_b.em == 30 and a1.limit <= a1.mid
    assert a1.slippage_per_side == pytest.approx(0.10)
    wide = _a1_chain(long_ask=58)
    skipped, why = build_order_plan(bo, wide, 7000, "A1")
    assert why == "slippage" and skipped.skip is True
    # a missing strike for the active target refuses the order and names it
    straddle_only = {
        (7000, "C"): OptQuote(14.5, 15.5, delta=0.50, con_id=70001),
        (7000, "P"): OptQuote(14.5, 15.5, delta=-0.50, con_id=70002),
    }
    assert build_order_plan(bo, straddle_only, 7000) == (None, "no 75-delta strike")
    assert build_order_plan(bo, straddle_only, 7000, "A1") == (None, "no 80-delta strike")


def _a1_chain(long_ask: float = 52.2):
    """B's chain plus the 80-delta strike and the short leg 30 points above it."""
    chain = _chain(long_ask=40.2)
    chain[(6925, "C")] = OptQuote(52.0, long_ask, delta=0.80, con_id=69251)
    chain[(6955, "C")] = OptQuote(34.0, 34.2, delta=0.60, con_id=69551)
    return chain


def test_the_a1_point_exit_is_eight_points_in_the_trade_direction():
    assert A1_EXIT_POINTS == 8.0
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
