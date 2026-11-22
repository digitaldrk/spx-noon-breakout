"""The trading-day gate: the hard-coded 2026–2027 list, then the broker's hours.

Nothing here reaches a broker. ``trading_day_status`` takes the hours string the
session already asks ``spx_hours`` for, so a closed day, a short day, and a day
with no hours at all are three arguments rather than three connections.

The list in ``spx_noon/calendar.py`` covers 2026 and 2027. A weekday after it is
``UNKNOWN`` to the list, and the broker's own session hours decide whether that
day may trade.
"""
from datetime import date

from spx_noon.calendar import KNOWN_YEARS, fallback_status, trading_day_status

#: A Monday in a year the hard-coded list does not cover.
UNLISTED = date(2028, 6, 5)
#: An ordinary Monday it does cover.
LISTED = date(2026, 11, 16)
#: Thanksgiving 2026, on the list.
HOLIDAY = date(2026, 11, 26)

ET_TZ = "America/New_York"
REGULAR = "20280605:0930-20280605:1600"
EARLY = "20280605:0930-20280605:1300"
SHUT = "20280605:CLOSED"
#: Hours for the Friday before, and none for the day being asked about.
ANOTHER_DAY = "20280602:0930-20280602:1600"


def test_the_hard_coded_list_covers_2026_and_2027_only():
    assert KNOWN_YEARS == {2026, 2027}
    assert fallback_status(LISTED) == ("OPEN", "regular session")
    status, reason = fallback_status(UNLISTED)
    assert status == "UNKNOWN" and "2028" in reason
    assert fallback_status(date(2028, 6, 3))[0] == "CLOSED"     # a weekend is still a weekend


def test_a_weekday_past_the_list_trades_on_regular_broker_hours():
    got = trading_day_status(UNLISTED, REGULAR, ET_TZ)
    assert got["status"] == "OPEN" and got["trade_allowed"] is True
    assert got["ib_status"] == "OPEN" and got["fallback_status"] == "UNKNOWN"
    assert got["ib_close_et"].startswith("2028-06-05T16:00")


def test_a_weekday_past_the_list_does_not_trade_when_the_broker_says_closed():
    got = trading_day_status(UNLISTED, SHUT, ET_TZ)
    assert got["status"] == "CLOSED" and got["trade_allowed"] is False
    assert got["ib_status"] == "CLOSED"


def test_a_weekday_past_the_list_does_not_trade_on_an_early_close():
    got = trading_day_status(UNLISTED, EARLY, ET_TZ)
    assert got["status"] == "EARLY" and got["trade_allowed"] is False
    assert got["ib_status"] == "EARLY"
    assert got["ib_close_et"].startswith("2028-06-05T13:00")


def test_a_weekday_past_the_list_does_not_trade_with_no_broker_hours():
    for missing in (None, ""):
        got = trading_day_status(UNLISTED, missing, ET_TZ)
        assert got["status"] == "UNKNOWN" and got["trade_allowed"] is False
        assert got["ib_status"] == "UNAVAILABLE"
    unlisted_hours = trading_day_status(UNLISTED, ANOTHER_DAY, ET_TZ)
    assert unlisted_hours["ib_status"] == "NOT_LISTED"
    assert unlisted_hours["status"] == "UNKNOWN"
    assert unlisted_hours["trade_allowed"] is False


def test_a_listed_holiday_stays_closed_even_on_regular_broker_hours():
    """The list is not overruled inside the years it covers."""
    got = trading_day_status(HOLIDAY, "20261126:0930-20261126:1600", ET_TZ)
    assert got["status"] == "CLOSED" and got["trade_allowed"] is False
    assert got["ib_status"] == "OPEN" and got["fallback_reason"] == "NYSE holiday"
