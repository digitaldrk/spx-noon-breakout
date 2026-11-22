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
8. **The exit.** There is no resting premium order. The spread leaves at +8 SPX points (item 11), at the two-hour flat, or at the 3:50 PM ET hard flat, whichever comes first. All three sell at the spread bid, and a second sell is not sent while an exit order is still working. The 3:50 hard flat is the backstop for a spread the first two did not sell: its row reads `hard_flat`, it clears the three-attempt counter once so it has three sells of its own, and an order still working at 3:50 is left working. The decided rules are in the trading plan.
9. **Fees.** P&L subtracts $0.65 per contract per leg on the open and again on the close ($2.60 for one spread). That matches the published +$378 on a $1,271 debit within a dollar.
10. **Late start.** A close is acted on only during the 90 seconds after it prints. A noon bar that is already stale when the process starts is not sent late. Missing or stale option quotes inside that window are retried; the bar is dropped only when the window is about to end. After tastytrade auto-approves, the order is rebuilt from fresh quotes and is not sent if the window has closed or the spread no longer qualifies.
11. **Once open.** A missing SPX print is not a +8 hit. After the print has reached +8, the exit stays on if the print comes back, including across a restart. A position file from an older build is managed the same way.
