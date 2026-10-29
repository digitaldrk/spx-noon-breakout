# Noon Breakout Plan v2 — 2026-10-28

The bot implements **version B only** (section 1 and the checklist in section 8). Version A is recorded below and is not coded. Candle Boy is a different repo.


*Sources: `filter-backtests-2026-10-28.md`, `filter-bt/stress/stress_out.md`, `filter-bt/noon_B_trades_k0.6.csv` and `filter-bt/noon_B_trades_k0.42.csv`. Sample: 181 SPX sessions, 2026-02-09 to 2026-10-27, 118 trades. Two pricing levels: **fair** = 0.42× VIX (**the default scenario used throughout**) and **rich** = 0.6× VIX (secondary reference). Read-only research. No bot code was changed.*

> **Bottom line:** Run version **B** (75-delta vertical, +30% target, flat 2h after entry, no stop) on **paper first**, with real option chains, and log every fill against mid. At fair pricing: 118 trades, 69% win rate, net **+$7,492** (PF 1.41, about $63 per trade), or **about +75% on $10k** before real slippage. It passed every stress test until slippage reached $0.30 a side.

---

## 1. Strategy to run (version B)

| Rule | Setting |
|---|---|
| Trigger | 15-minute close beyond the prior day's high (call) or low (put), from noon to 1:00 PM ET. One fill per day. |
| Entry | The 12:00 15-minute close. 100 of 118 trades entered then; the rest came later in the window. |
| Structure | Debit vertical. Long leg at **75 delta**. |
| Short leg | Width = **max(10, expected move rounded down to 5)** |
| Take-profit | **+30%** of debit |
| Time exit | **Flat 2 hours after entry** |
| Stop | **None** |

**Dropped: version A (70 delta, +17% target).** It failed the stress test. It lost money in Jun–Oct, in low- and mid-VIX sessions, in up-trends, when the entry shifted ±15 minutes, and at about $0.10 slippage.

**Correction to v1:** in the earlier hold-to-close test, holding to the close **lost money in every window**. That is why the 2-hour time exit is part of the rules.

---

## 2. Results (1 spread per trade, 118 trades, ~8.5 months)

| Pricing | Win rate | Avg win | Avg loss | Net | PF | Per trade | Max drawdown (cumulative) |
|---|---|---|---|---|---|---|---|
| Fair (0.42× VIX) | 69% | $317 | −$492 | **+$7,492** | 1.41 | ~$63 | −$3,073 (2026-06-30 to 07-27) |
| *Rich (0.6× VIX), reference* | *62%* | *$412* | *−$453* | *+$9,694* | *1.48* | *~$82* | *−$4,665 (05-11 to 07-29)* |

On a $10,000 account that is **about +75% at fair pricing** (headline), versus about +97% at rich pricing, both before real slippage.

---

## 3. Stress test of B

| Test | PF: **fair** / rich |
|---|---|
| Halves | Positive in both. Jun–Oct alone: **1.06** / 1.30 |
| VIX tercile | Positive in all three (low, mid, high) |
| Trend | Positive in up-trends and down-trends |
| Entry 15m earlier (11:45) | 1.07 / 1.15 |
| Entry 15m later (12:15) | 1.03 / 1.31 |
| VIX input ±3 pts | Holds (lowest PF 1.27) |
| Slippage $0.20/side | 1.10 / 1.23 |
| Slippage $0.30/side | **Breaks: 0.94** / 1.10 |

**Weak spots:** the edge is thin from June on, and 4 of the 9 months were red. **Sensitivity:** fill slippage matters most, then the time period. The VIX input matters least.

---

## 4. Sizing for a $10,000 account

**1 spread per trade.**

| Item | **Fair (0.42×), default** | Rich (0.6×), reference |
|---|---|---|
| Debit, median | ~$1,220 | ~$1,770 |
| Debit, range | $919–$1,880 | $1,270–$2,513 |
| Typical width | 15–20 pts | 25–30 pts |
| Worst realized loss | −$1,228 (2026-03-03, 6815/6790 put) | −$1,348 (2026-05-19, 7375/7345 put) |

- **Worst drawdown during a trade:** −$1,369 at fair (2026-03-03); −$1,655 at rich (2026-07-08). **Median drawdown:** about −$300.
- **Max drawdown of cumulative P&L:** −$3,073 at fair (2026-06-30 to 2026-07-27); −$4,665 at rich (2026-05-11 to 2026-07-29).
- **No trade lost the full debit.** The 2-hour exit always left some value in the spread.

**Example trades (fair pricing)**

| Type | Date | Spread | Result |
|---|---|---|---|
| Winner | 2026-07-31 | 7435/7455 call, debit $1,271 | Target hit, **+$378** (rich: 7430/7460, +$532) |
| Typical loser | 2026-09-16 | 7600/7620 call, debit $1,252 | −$129, time exit (rich: 7595/7625, −$135) |
| Bad loser | 2026-04-16 | 7030/7050 call, debit $1,198 | −$597, time exit (rich: 7020/7050, −$577) |

---

## 5. Recommendation

1. **Paper trade B on real option chains** before using real money.
2. **Log every fill against mid.** Typical 0DTE fills come in $0.05–$0.15 off mid. The backtest breaks at $0.30 a side, so real slippage decides whether this works.
3. Stay at 1 spread per trade on $10k.

---

## 6. Caveats

- Option prices are **Black-Scholes estimates from SPX and VIX** (scaled 0.42× and 0.6×, no skew), **not real fills**. The direction is reliable; the dollar amounts are approximate.
- Costs modeled: $0.05 slippage per leg plus $0.65 per contract per leg.

---

## 7. Candle Boy

Candle Boy keeps trading his 10:00–11:00 bars. **He is not part of this plan.**

---

## 8. Implementation checklist (for Derek)

*A step-by-step spec you can code by hand, with AI, or in Cursor. Steps 2–4 follow the backtest code exactly (`filter-bt/noon.py` `gen()`/`sim2()`, helpers in `bt.py`, `win.py`, `loss.py`). Times are ET.*

1. **Data needed**
    - SPX 15-minute RTH bars (9:30–16:00 ET, open/high/low/close), live intraday plus the prior session.
    - Prior day's RTH high (PDH) and low (PDL): the max high and min low of yesterday's 15m RTH bars.
    - 0DTE SPX option chain with bid, ask, mid and delta.
    - VIX (live) or the 0DTE ATM straddle, for the expected move. The backtest used EM = the price of the ATM straddle (strike = spot rounded to 5) at entry.
2. **Entry window.** Check the 15m bars that close at **12:00, 12:15, 12:30, 12:45 and 1:00 PM**. Take the first bar that qualifies. Most entries (100/118) came on the 12:00 close; the rest came on the later closes.
3. **Breakout rule.** Check calls first, then puts. A bar qualifies for **calls** when all three of these are true (mirror everything for puts, using PDL and "below"):
    - a. At least one 15m close earlier today, from 9:45 on, was above PDH. The first such bar is the **trigger bar**. It can be in the morning.
    - b. This bar's close is still above PDH.
    - c. This bar's close is above the trigger bar's **low** (trigger bar's high for puts).
    - *No moving-average check.* The backtest's 20-period EMA gate was tested on vs. off: identical 118 trades, same days, same results (PF 1.48 rich / 1.41 fair, same drawdowns). Only 4 closes ever failed it and none changed an entry. Dropped to simplify the code.
4. **Max 1 trade per day.** Stop checking for the day after one entry.
5. **Long leg:** the strike whose delta is nearest **0.75** (in the money), in 5-point strikes.
6. **Short leg:** W = max(10, floor(expected_move / 5) × 5) points further out of the money (long + W for calls, long − W for puts).
7. **Size:** 1 spread on a $10,000 account.
8. **Order:** a limit order for the debit spread at **mid**. If the fill would be **more than $0.20 per side worse than mid**, skip the trade. Do not chase. (The backtest breaks at $0.30 a side.)
9. **Target exit:** right after the fill, place a GTC closing limit at **debit × 1.30**. Exit when the spread **bid ≥ debit × 1.30**.
10. **Hard time exit:** close at market or near bid **2 hours after entry**. The backtest exits on the close of the 15m bar that ends at entry + 2h (12:00 entry → 2:00 PM). There is **no stop loss**.
11. **Paper mode first.** For every trade, log: date, timestamp, direction, SPX at entry, PDH/PDL, expected move, W, both strikes, both deltas, spread mid, fill price, **slippage versus mid**, exit time, exit reason (target or time), exit fill, slippage on the exit, and P&L after $0.65 per contract per leg.
12. **Slippage benchmark:** typical 0DTE fills come in $0.05–$0.15 off mid. Track the average per side. Anything at $0.20 or more erodes the edge, and $0.30 breaks it.
13. **Review after 30–60 paper sessions** before going live: win rate, profit factor, and average slippage against the backtest (PF 1.41 at fair pricing; 1.48 rich).
14. **Live orders require Tod's explicit approval.** The code must not send live orders unless Tod has approved going live.
