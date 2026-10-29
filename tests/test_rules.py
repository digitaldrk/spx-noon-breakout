"""Entry, width, delta, slippage, and exit rules from plan v2 version B."""
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
    Bar,
    OptQuote,
    atm_strike,
    build_order_plan,
    pdh_pdl,
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
