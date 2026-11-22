# Readings

Add an entry when a ticket or the trading plan leaves a mechanical choice undecided. The entry tells Derek and his Tod what was unclear and which reading the bot is using until they decide.

A decided rule belongs in the trading plan. An acceptance criterion may require a new entry here only when the work order names a choice the ticket and the plan leave open.

1. **Per side.** Slippage per side is `(marketable debit − spread mid) / 2`. The trade is skipped when that is greater than $0.20. $0.20 exactly is still taken. The order itself is the mid, rounded down to the nearest $0.05, so a sent order does not bid through the mid.
2. **A skip is not a fill.** If the first qualifying bar is too wide, a later bar in the noon window can still be the one trade. An unfilled entry is cancelled at the next 15-minute close and is not chased. A cancel that the broker has not confirmed stays on the books; the bot does not forget it and does not send a second entry. One fill ends the day, including after a restart: a closed fill in `state/own_position.json` blocks another entry that day.
3. **Two hours.** The flat is the entry bar's close plus two hours (12:00 → 2:00 PM), which is the backtest clock. It is not two hours after the fill timestamp.
4. **80 delta.** Absolute delta, in the money only (calls below spot, puts above spot). A tie goes to the strike closer to spot. Puts may be quoted with a negative delta; the sign is ignored for the distance to 0.80.
5. **Above / below** are strict. A close equal to PDH or PDL does not qualify. Calls are tested before puts. The same strict test is the morning skip.
6. **The signal.** The window close beyond PDH or PDL is the whole signal. No earlier bar is required. The break bar is the bar the note records. Bar timestamps from both brokers are bar starts, same as the other bots.
7. **Spot for the straddle** is the live SPX print at the decision, rounded half away from zero to the 5-point strike. The expected move is that straddle's mid, in points.
