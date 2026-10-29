# Readings

The bot follows plan v2 version B. These are the spots where the checklist does not spell out a mechanical detail.

1. **Per side.** Slippage per side is `(marketable debit − spread mid) / 2`. The trade is skipped when that is greater than $0.20. $0.20 exactly is still taken. The order itself is the mid, rounded down to the nearest $0.05, so a sent order does not bid through the mid.
2. **A skip is not a fill.** If the first qualifying bar is too wide, a later bar in the noon window can still be the one trade. An unfilled entry is cancelled at the next 15-minute close and is not chased. One fill ends the day.
3. **Two hours.** The flat is the entry bar's close plus two hours (12:00 → 2:00 PM), which is the backtest clock. It is not two hours after the fill timestamp.
4. **75 delta.** Absolute delta, in the money only (calls below spot, puts above spot). A tie goes to the strike closer to spot. Puts may be quoted with a negative delta; the sign is ignored for the distance to 0.75.
5. **Above / below** are strict. A close equal to PDH, PDL, or the trigger extreme does not qualify. Calls are tested before puts.
6. **Trigger.** The trigger is the first 15-minute close at or after 9:45, and strictly before the entry bar. The entry bar cannot be its own trigger. Bar timestamps from both brokers are bar starts, same as the other bots.
7. **Spot for the straddle** is the live SPX print at the decision, rounded half away from zero to the 5-point strike. The expected move is that straddle's mid, in points.
8. **Take-profit order.** The resting limit is `debit × 1.30` floored to $0.05, so a bid at the exact multiple can fill it. The exit reason is `target` when the spread bid is at or through `debit × 1.30`, or when that resting order fills. The two-hour exit sells at the spread bid.
9. **Fees.** P&L subtracts $0.65 per contract per leg on the open and again on the close ($2.60 for one spread). That matches the published +$378 on a $1,271 debit within a dollar.
10. **Late start.** A close is acted on only during the 90 seconds after it prints. A noon bar that is already stale when the process starts is not sent late.
11. **Live gate.** Mac `paper` may send to TWS port 7497. Windows sends only in `live`, and only after `yes <ticketID>`. `run_session.ps1` defaults to dry. There is no config key that turns a Mac into Schwab or a PC into IBKR.
12. **Early close and holidays.** If the NYSE list or the broker hours say closed or early close, the day is flat. The noon window runs into the 1:00 PM early close, so those days are not traded.
13. **Wrap.** The 4:20 job summarizes `state/trades.csv`. It does not rebuild P&L from broker fills and it does not send orders.
