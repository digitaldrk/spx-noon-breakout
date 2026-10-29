"""Trading-day check. Broker hours first, then the NYSE 2026–27 list.

If either source says closed or early close, the bot does not trade.
The noon window ends at 1:00 PM, so a 1:00 PM early close is not a session we trade.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

NYSE_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}

NYSE_EARLY_CLOSES = {
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26), date(2027, 12, 23),
}

KNOWN_YEARS = {2026, 2027}


def fallback_status(day: date) -> tuple[str, str]:
    if day.weekday() >= 5:
        return "CLOSED", "weekend"
    if day in NYSE_HOLIDAYS:
        return "CLOSED", "NYSE holiday"
    if day in NYSE_EARLY_CLOSES:
        return "EARLY", "NYSE early close (1:00 PM)"
    if day.year not in KNOWN_YEARS:
        return "UNKNOWN", f"no hard-coded holiday list for {day.year}"
    return "OPEN", "regular session"


def parse_ib_hours(hours: str, tz_id: str, day: date) -> tuple[str, datetime | None, datetime | None]:
    tz = ZoneInfo(_norm_tz(tz_id))
    key = day.strftime("%Y%m%d")
    opens, closes, closed = [], [], False
    for seg in (hours or "").split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if seg.endswith("CLOSED"):
            if seg.startswith(key):
                closed = True
            continue
        try:
            left, right = seg.split("-")
            da, ta = left.split(":")
            db, tb = right.split(":")
        except ValueError:
            continue
        opened = datetime.strptime(da + ta, "%Y%m%d%H%M").replace(tzinfo=tz).astimezone(ET)
        closed_at = datetime.strptime(db + tb, "%Y%m%d%H%M").replace(tzinfo=tz).astimezone(ET)
        if closed_at.date() == day:
            opens.append(opened)
            closes.append(closed_at)
    if opens:
        return "OPEN", min(opens), max(closes)
    if closed:
        return "CLOSED", None, None
    return "NOT_LISTED", None, None


def _norm_tz(tz_id: str) -> str:
    aliases = {
        "EST": "America/New_York", "EST5EDT": "America/New_York",
        "CST": "America/Chicago", "CST6CDT": "America/Chicago",
        "US/Central": "America/Chicago", "US/Eastern": "America/New_York",
    }
    return aliases.get(tz_id, tz_id or "America/New_York")


def trading_day_status(day: date, ib_hours: str | None = None, ib_tz: str | None = None) -> dict:
    fallback, fallback_reason = fallback_status(day)
    ib_status, ib_close = "UNAVAILABLE", None
    if ib_hours:
        status, _opened, closed_at = parse_ib_hours(ib_hours, ib_tz or "America/New_York", day)
        if status == "CLOSED":
            ib_status = "CLOSED"
        elif status == "OPEN":
            ib_close = closed_at
            ib_status = "EARLY" if closed_at.time() < time(16, 0) else "OPEN"
        else:
            ib_status = "NOT_LISTED"
    statuses = {fallback, ib_status}
    if "CLOSED" in statuses:
        final = "CLOSED"
    elif "EARLY" in statuses:
        final = "EARLY"
    elif ib_status == "OPEN" and fallback in ("OPEN", "UNKNOWN"):
        final = "OPEN"
    elif ib_status in ("UNAVAILABLE", "NOT_LISTED") and fallback == "OPEN":
        final = "OPEN"
    else:
        final = "UNKNOWN"
    return {
        "status": final,
        "trade_allowed": final == "OPEN",
        "ib_status": ib_status,
        "ib_close_et": ib_close.isoformat() if ib_close else None,
        "fallback_status": fallback,
        "fallback_reason": fallback_reason,
    }
