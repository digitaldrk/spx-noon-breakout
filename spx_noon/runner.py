"""One noon session.

Waits for the 12:00–1:00 PM ET closes, takes the first qualifying bar, and
sends at most one spread. Paper on a Mac goes to TWS 7497. Live on Windows
waits for `yes <ticketID>` and otherwise does not send.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .calendar import trading_day_status
from .config import SafetyError, load_config
from .logbook import Outputs
from .notify import ConsoleApprover, Notifier
from .pricing import floor_tick, take_profit_limit, target_reached, time_exit_deadline, trade_pnl
from . import resume
from .strategy import (
    build_order_plan,
    decision_closes,
    lookup_quote,
    pdh_pdl,
    qualifies,
    session_bars,
    bar_closing_at,
    WINDOW_CLOSES,
)
from .pricing import spread_prices

ET = ZoneInfo("America/New_York")
log = logging.getLogger("spx_noon.runner")


@dataclass
class _State:
    working: dict | None = None
    position: dict | None = None
    consumed: set = field(default_factory=set)
    seq: int = 1
    done_for_day: bool = False
    halt: bool = False


def _setup_logging(cfg) -> None:
    cfg.ensure_dirs()
    path = cfg.state_dir / f"bot_{datetime.now(ET):%Y-%m-%d}.log"
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.FileHandler(path), logging.StreamHandler()],
        )


def _resolve_account(cfg, broker) -> str:
    if cfg.broker == "schwab":
        return cfg.account
    accounts = list(broker.managed_accounts() or [])
    if cfg.account:
        if accounts and cfg.account not in accounts and cfg.broker == "ibkr":
            raise SafetyError(f"account {cfg.account} is not managed by TWS ({accounts})")
        return cfg.account
    if not accounts:
        raise SafetyError("broker reported no account")
    return str(accounts[0])


def _assert_paper_accounts(broker) -> None:
    bad = [acct for acct in broker.managed_accounts() if not str(acct).startswith("D")]
    if bad:
        raise SafetyError(f"refusing non-paper account(s) {bad}")


def _connect(cfg, broker) -> None:
    attempts = 1 if cfg.broker == "sim" else max(1, int(cfg.connect_retries))
    last = None
    for n in range(attempts):
        try:
            broker.connect()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("connect failed (%s/%s): %s", n + 1, attempts, exc)
            if n + 1 < attempts:
                broker.sleep(30)
    raise SafetyError(f"could not connect: {last}")


def _filled(state: dict | None) -> bool:
    return bool(state and state.get("status") == "Filled" and float(state.get("filled") or 0) >= 1
                and float(state.get("avg_price") or 0) > 0)


def _safe_state(broker, order_id):
    if order_id is None:
        return None
    try:
        return broker.order_state(order_id)
    except Exception:  # noqa: BLE001
        return None


def _next_decision(close_at: datetime, grace: timedelta) -> datetime:
    current = close_at.timetz().replace(tzinfo=None)
    for stamp in WINDOW_CLOSES:
        if stamp > current:
            return datetime.combine(close_at.date(), stamp, close_at.tzinfo)
    return close_at + grace


def _subscribe(cfg, broker, spot: float, expiry: str) -> None:
    strikes = broker.chain_strikes(expiry) or []
    band = [strike for strike in strikes if abs(float(strike) - spot) <= cfg.chain_band_pts]
    band = sorted(band, key=lambda strike: abs(float(strike) - spot))[: cfg.max_option_lines]
    if band:
        broker.ensure_options(expiry, band)


def _prices_for(quotes, pos) -> dict | None:
    long_q = lookup_quote(quotes, pos["long_strike"], pos["right"])
    short_q = lookup_quote(quotes, pos["short_strike"], pos["right"])
    if long_q is None or short_q is None:
        return None
    return spread_prices(long_q, short_q)


def _ticket_text(plan) -> str:
    return "\n".join([
        f"{plan.direction} debit vertical  1 spread",
        f"SPX {plan.spot:.2f}   PDH {plan.pdh:.2f}   PDL {plan.pdl:.2f}",
        f"expected move {plan.em:.2f}   width {plan.width}",
        f"long {plan.long_strike:.0f}{plan.right} Δ {plan.long_delta:.3f}",
        f"short {plan.short_strike:.0f}{plan.right} Δ {plan.short_delta if plan.short_delta is not None else float('nan'):.3f}",
        f"mid {plan.mid:.2f}   natural {plan.natural:.2f}   slippage {plan.slippage_per_side:.3f}/side",
        f"limit {plan.limit:.2f}   target {plan.limit * 1.30:.2f} then flat at {time_exit_deadline(plan.entry_close):%H:%M} ET",
        "No stop. Live send still needs yes <ticket>.",
    ])


def _base_row(pos, mode: str) -> dict:
    return {
        "date": pos["entry_close"].date(),
        "timestamp": pos["entry_close"],
        "direction": pos["direction"],
        "spx": pos["spx"],
        "pdh": pos["pdh"],
        "pdl": pos["pdl"],
        "expected_move": pos["em"],
        "width": pos["width"],
        "long_strike": pos["long_strike"],
        "short_strike": pos["short_strike"],
        "long_delta": pos["long_delta"],
        "short_delta": pos["short_delta"],
        "spread_mid": pos["mid"],
        "mode": mode,
        "note": pos.get("note", ""),
    }


def _persist(cfg, pos) -> None:
    payload = dict(pos)
    for key in ("entry_close", "deadline", "cancel_after"):
        if isinstance(payload.get(key), datetime):
            payload[key] = payload[key].isoformat()
    resume.save(cfg.state_dir, payload)


def _restore_dt(value):
    if isinstance(value, datetime) or value in (None, ""):
        return value
    return datetime.fromisoformat(str(value))


def _row_from_plan(plan, mode: str, reason: str, **extra) -> dict:
    pos = _pos_from_plan(plan, fill=None)
    row = _base_row(pos, mode)
    row["fill_price"] = extra.get("fill_price", "")
    row["slippage_vs_mid"] = extra.get("slippage_vs_mid", plan.natural - plan.mid)
    row["slippage_per_side"] = extra.get("slippage_per_side", plan.slippage_per_side)
    row["exit_reason"] = reason
    row["exit_time"] = extra.get("exit_time", "")
    row["exit_fill"] = extra.get("exit_fill", "")
    row["pnl"] = extra.get("pnl", "")
    row["fees"] = extra.get("fees", "")
    return row


def _pos_from_plan(plan, fill, **extra) -> dict:
    entry_close = plan.entry_close
    pos = {
        "date": entry_close.date().isoformat(),
        "status": "working",
        "direction": plan.direction,
        "right": plan.right,
        "spx": plan.spot,
        "pdh": plan.pdh,
        "pdl": plan.pdl,
        "em": plan.em,
        "width": plan.width,
        "long_strike": plan.long_strike,
        "short_strike": plan.short_strike,
        "long_delta": plan.long_delta,
        "short_delta": plan.short_delta,
        "mid": plan.mid,
        "limit": plan.limit,
        "entry_close": entry_close,
        "deadline": time_exit_deadline(entry_close),
        "legs": plan.legs(),
        "note": (
            f"trigger {plan.trigger_close.isoformat(timespec='minutes')} "
            f"low {plan.trigger_low:.2f} high {plan.trigger_high:.2f}"
        ),
        "tp_order_id": None,
        "entry_order_id": None,
        "fill_price": fill,
        "tp_limit": None,
    }
    pos.update(extra)
    return pos


def _open_position(cfg, broker, notifier, state: _State, working: dict, fill: float) -> None:
    plan = working["plan"]
    tp_limit = take_profit_limit(fill)
    pos = _pos_from_plan(
        plan, fill,
        status="open",
        fill_price=fill,
        tp_limit=tp_limit,
        account=working["account"],
        ref=working["ref"],
        ticket=working.get("ticket"),
        entry_order_id=working["id"],
        mode=cfg.mode,
    )
    state.working = None
    state.done_for_day = True
    state.position = pos
    _persist(cfg, pos)
    if cfg.sends_orders():
        pos["tp_order_id"] = broker.place_combo(
            pos["legs"], "SELL", 1, tp_limit, pos["account"], pos["ref"] + "X", tif="GTC")
        _persist(cfg, pos)
    notifier.alert(
        "Noon filled",
        f"{pos['direction']} filled {fill:.2f} (mid was {plan.mid:.2f}). "
        f"GTC close at {tp_limit:.2f}. Flat at {pos['deadline']:%H:%M} ET. No stop.",
        toast=cfg.mode != "dry",
    )


def _record_exit(cfg, broker, notifier, out: Outputs, state: _State, exit_fill: float,
                 reason: str, prices: dict | None) -> None:
    pos = state.position
    now = broker.now()
    pnl, fees = trade_pnl(pos["fill_price"], exit_fill)
    entry_slip = pos["fill_price"] - pos["mid"]
    exit_mid = None if prices is None else prices.get("mid")
    exit_slip = None if exit_mid is None else exit_mid - exit_fill
    row = _base_row(pos, cfg.mode)
    row.update({
        "fill_price": pos["fill_price"],
        "slippage_vs_mid": entry_slip,
        "slippage_per_side": entry_slip / 2.0,
        "exit_time": now,
        "exit_reason": reason,
        "exit_fill": exit_fill,
        "exit_slippage_vs_mid": exit_slip if exit_slip is not None else "",
        "exit_slippage_per_side": (exit_slip / 2.0) if exit_slip is not None else "",
        "pnl": pnl,
        "fees": fees,
    })
    out.append_trade(row)
    resume.mark_closed(cfg.state_dir)
    if cfg.live:
        try:
            broker.finish_ticket()
        except Exception:  # noqa: BLE001
            log.warning("finish_ticket failed", exc_info=True)
    notifier.alert(
        f"Noon exit {reason}",
        f"exit {exit_fill:.2f}  pnl {pnl:.2f} after fees {fees:.2f}",
        toast=cfg.mode != "dry",
    )
    state.position = None


def _flatten(cfg, broker, notifier, out, state, reason: str, prices: dict | None) -> bool:
    pos = state.position
    bid = None if prices is None else prices.get("close_bid")
    if bid is None or bid <= 0:
        if broker.now() < pos["deadline"] + timedelta(seconds=cfg.exit_wait_s):
            log.info("waiting for a positive spread bid to flatten")
            return False
        limit = 0.05
    else:
        limit = max(0.05, floor_tick(bid))
    order_id = broker.place_combo(
        pos["legs"], "SELL", 1, limit, pos["account"], pos["ref"] + "F", tif="DAY")
    wait_until = broker.now() + timedelta(seconds=cfg.exit_wait_s)
    while True:
        status = broker.order_state(order_id)
        if _filled(status):
            fresh = _prices_for(broker.option_quotes(), pos)
            _record_exit(cfg, broker, notifier, out, state, status["avg_price"], reason, fresh or prices)
            return True
        if broker.now() >= wait_until:
            break
        broker.sleep(1)
    try:
        broker.cancel(order_id)
    except Exception:  # noqa: BLE001
        log.warning("cancel flatten failed", exc_info=True)
    status = _safe_state(broker, order_id)
    if _filled(status):
        _record_exit(cfg, broker, notifier, out, state, status["avg_price"], reason, prices)
        return True
    log.warning("flatten order %s did not fill at %.2f", order_id, limit)
    return False


def _manage_position(cfg, broker, notifier, out, state: _State) -> bool:
    """Return True when the spread is flat."""
    pos = state.position
    now = broker.now()
    prices = _prices_for(broker.option_quotes(), pos)
    tp_id = pos.get("tp_order_id")
    tp_state = _safe_state(broker, tp_id)
    if _filled(tp_state):
        _record_exit(cfg, broker, notifier, out, state, tp_state["avg_price"], "target", prices)
        return True
    hit = prices is not None and target_reached(prices["close_bid"], pos["fill_price"])
    if hit or now >= pos["deadline"]:
        reason = "target" if hit else "time"
        if tp_id is not None:
            try:
                broker.cancel(tp_id)
            except Exception:  # noqa: BLE001
                log.warning("cancel target failed", exc_info=True)
            tp_state = _safe_state(broker, tp_id)
            if _filled(tp_state):
                _record_exit(cfg, broker, notifier, out, state, tp_state["avg_price"], "target", prices)
                return True
        return _flatten(cfg, broker, notifier, out, state, reason, prices)
    return False


def _poll_working(cfg, broker, notifier, out, state: _State) -> None:
    working = state.working
    status = _safe_state(broker, working["id"])
    if _filled(status):
        _open_position(cfg, broker, notifier, state, working, status["avg_price"])
        return
    if broker.now() < working["cancel_after"]:
        return
    try:
        broker.cancel(working["id"])
    except Exception:  # noqa: BLE001
        log.warning("cancel entry failed", exc_info=True)
    status = _safe_state(broker, working["id"])
    if _filled(status):
        _open_position(cfg, broker, notifier, state, working, status["avg_price"])
        return
    out.append_trade(_row_from_plan(working["plan"], cfg.mode, "skip_no_fill"))
    notifier.alert("Noon no fill", f"entry {working['ref']} cancelled unfilled. Not chasing.", toast=False)
    state.working = None
    resume.mark_closed(cfg.state_dir)


def _maybe_enter(cfg, broker, notifier, approver, out, state: _State, account: str, closes) -> None:
    if state.done_for_day or state.halt or state.working or state.position:
        return
    now = broker.now()
    grace = timedelta(seconds=cfg.decision_grace_s)
    for close_at in closes:
        if close_at in state.consumed:
            continue
        if now < close_at:
            return
        if now > close_at + grace:
            state.consumed.add(close_at)
            log.info("missed the %s close", close_at.strftime("%H:%M"))
            continue
        bars = broker.spx_bars_15m()
        bar = bar_closing_at(bars, close_at)
        if bar is None:
            log.info("15m bar closing %s is not in history yet", close_at.strftime("%H:%M"))
            return
        levels = pdh_pdl(bars, close_at.date())
        if levels is None:
            state.consumed.add(close_at)
            log.info("no prior session for %s", close_at.date())
            continue
        pdh, pdl, _session = levels
        today = session_bars(bars, close_at.date(), now)
        breakout = qualifies(bar, today, pdh, pdl)
        if breakout is None:
            state.consumed.add(close_at)
            log.info("no breakout on the %s close", close_at.strftime("%H:%M"))
            continue
        spot = broker.spx_px().value
        if spot is None:
            log.info("SPX quote missing; will retry the %s close", close_at.strftime("%H:%M"))
            return
        expiry = close_at.strftime("%Y%m%d")
        try:
            _subscribe(cfg, broker, float(spot), expiry)
            quotes = broker.option_quotes()
        except Exception as exc:  # noqa: BLE001
            log.warning("chain unavailable: %s", exc)
            return
        plan, reason = build_order_plan(breakout, quotes, float(spot))
        state.consumed.add(close_at)
        if plan is None:
            log.info("no order at %s: %s", close_at.strftime("%H:%M"), reason)
            continue
        if plan.skip:
            out.append_trade(_row_from_plan(plan, cfg.mode, "skip_slippage"))
            notifier.alert(
                "Noon skip",
                f"slippage {plan.slippage_per_side:.3f}/side is over 0.20. Not chasing.",
                toast=False,
            )
            continue
        if not cfg.sends_orders():
            out.append_trade(_row_from_plan(plan, cfg.mode, "dry_signal", slippage_vs_mid="", slippage_per_side=""))
            notifier.alert(
                "Noon signal (not sent)",
                _ticket_text(plan),
                toast=cfg.mode == "alerts",
            )
            state.done_for_day = True
            return
        ticket = None
        if cfg.live:
            ticket = f"N{now:%H%M%S}"
            try:
                preview = broker.preview_combo(plan.legs(), "BUY", 1, plan.limit, tif="DAY")
            except Exception as exc:  # noqa: BLE001
                log.warning("preview failed: %s", exc)
                preview = {"ok": False, "rejects": [str(exc)]}
            notifier.alert("Noon ticket " + ticket, _ticket_text(plan))
            if not preview.get("ok", False):
                out.append_trade(_row_from_plan(plan, cfg.mode, "skip_preview"))
                continue
            approved, why = approver.ask(ticket, cfg.approval_timeout_secs)
            if not approved:
                out.append_trade(_row_from_plan(plan, cfg.mode, "skip_approval"))
                notifier.alert("Noon skipped", f"{ticket} {why}", toast=False)
                continue
            broker.approve_ticket(ticket, plan.limit)
        ref = f"SPXNOON-{close_at:%Y%m%d}-{state.seq}"
        state.seq += 1
        try:
            order_id = broker.place_combo(plan.legs(), "BUY", 1, plan.limit, account, ref, tif="DAY")
        except Exception as exc:  # noqa: BLE001
            log.exception("entry order failed")
            notifier.alert("Entry failed", str(exc))
            if cfg.live:
                try:
                    broker.finish_ticket()
                except Exception:  # noqa: BLE001
                    pass
            state.halt = True
            return
        working = _pos_from_plan(plan, None, status="working", account=account, ref=ref, ticket=ticket,
                                 entry_order_id=order_id, mode=cfg.mode)
        working["id"] = order_id
        working["plan"] = plan
        working["cancel_after"] = _next_decision(close_at, grace)
        state.working = working
        _persist(cfg, {k: v for k, v in working.items() if k != "plan"})
        log.info("working entry %s limit %.2f", ref, plan.limit)
        return


def _sleep_for(cfg, broker, state: _State, closes) -> None:
    now = broker.now()
    step = timedelta(seconds=cfg.loop_interval_s)
    if state.position is not None:
        wake = min(state.position["deadline"], now + step)
    elif state.working is not None:
        wake = min(state.working["cancel_after"], now + step)
    else:
        pending = [c for c in closes if c not in state.consumed and c <= now]
        upcoming = [c for c in closes if c not in state.consumed and c > now]
        if pending:
            wake = now + timedelta(seconds=min(cfg.loop_interval_s, 5))
        elif upcoming:
            wake = upcoming[0]
        else:
            wake = now + step
    delay = (wake - now).total_seconds()
    broker.sleep(cfg.loop_interval_s if delay <= 0 else delay)


def _window_finished(broker, state: _State, closes, grace: timedelta) -> bool:
    now = broker.now()
    for close_at in closes:
        if close_at not in state.consumed and now <= close_at + grace:
            return False
    return state.working is None and state.position is None


def _load_saved(cfg, broker, state: _State, saved: dict) -> None:
    pos = dict(saved)
    pos["entry_close"] = _restore_dt(pos.get("entry_close"))
    pos["deadline"] = _restore_dt(pos.get("deadline"))
    if pos.get("cancel_after"):
        pos["cancel_after"] = _restore_dt(pos["cancel_after"])
    pos["legs"] = [tuple(leg) for leg in pos.get("legs") or []]
    pos["fill_price"] = pos.get("fill_price")
    if pos.get("status") == "working":
        order_id = pos.get("entry_order_id")
        status = _safe_state(broker, order_id)
        if _filled(status):
            dummy = {
                "plan": _plan_shim(pos),
                "account": pos.get("account"),
                "ref": pos.get("ref"),
                "ticket": pos.get("ticket"),
                "id": order_id,
            }
            _open_position(cfg, broker, Notifier(cfg.home / "logs", toast=False), state, dummy, status["avg_price"])
            return
        if status and not status.get("done"):
            pos["id"] = order_id
            pos["plan"] = _plan_shim(pos)
            state.working = pos
            return
        resume.mark_closed(cfg.state_dir)
        return
    state.done_for_day = True
    state.position = pos
    if pos.get("tp_order_id") and _filled(_safe_state(broker, pos["tp_order_id"])):
        return
    if cfg.sends_orders() and not pos.get("tp_order_id") and broker.now() < pos["deadline"]:
        pos["tp_limit"] = pos.get("tp_limit") or take_profit_limit(pos["fill_price"])
        pos["tp_order_id"] = broker.place_combo(
            pos["legs"], "SELL", 1, float(pos["tp_limit"]), pos["account"], str(pos["ref"]) + "X", tif="GTC")
        _persist(cfg, pos)


class _PlanShim:
    """Just enough of OrderPlan for a restart that already has a fill."""

    def __init__(self, pos):
        self.direction = pos["direction"]
        self.right = pos["right"]
        self.spot = pos["spx"]
        self.pdh = pos["pdh"]
        self.pdl = pos["pdl"]
        self.em = pos["em"]
        self.width = pos["width"]
        self.long_strike = pos["long_strike"]
        self.short_strike = pos["short_strike"]
        self.long_delta = pos["long_delta"]
        self.short_delta = pos.get("short_delta")
        self.mid = pos["mid"]
        self.limit = pos.get("limit") or pos["mid"]
        self.entry_close = pos["entry_close"]
        self.natural = pos["mid"]
        self.slippage_per_side = 0.0
        self.trigger_close = pos["entry_close"]
        self.trigger_low = 0.0
        self.trigger_high = 0.0
        self._legs = pos["legs"]

    def legs(self):
        return self._legs


def _plan_shim(pos):
    return _PlanShim(pos)


def _write_report(cfg, out: Outputs, day, calendar_status) -> None:
    rows = out.read_trades(day)
    lines = [
        f"# Noon breakout {day.isoformat()}",
        "",
        f"Mode `{cfg.mode}`. Session {calendar_status['status']}.",
        "",
    ]
    if not rows:
        lines.append("No signal and no fill.")
    for row in rows:
        lines.append(
            f"- {row.get('timestamp') or ''} {row.get('direction') or ''} "
            f"{row.get('exit_reason') or ''} pnl {row.get('pnl') or 'n/a'}"
        )
    lines.append("")
    lines.append("Version B: 75-delta debit, limit at mid, +30% target, flat two hours after the entry bar.")
    out.write_report(day, "\n".join(lines) + "\n")


def run_session(cfg, broker, approver=None, notifier=None) -> int:
    cfg.validate()
    cfg.ensure_dirs()
    notifier = notifier or Notifier(cfg.home / "logs", toast=cfg.toast and cfg.mode == "live")
    approver = approver or ConsoleApprover()
    out = Outputs(cfg.state_dir)
    _connect(cfg, broker)
    try:
        now = broker.now()
        day = now.date()
        try:
            hours, tz = broker.spx_hours()
        except Exception as exc:  # noqa: BLE001
            log.warning("market hours unavailable: %s", exc)
            hours, tz = None, None
        calendar_status = trading_day_status(day, hours, tz)
        out.write_status(day, {"calendar": calendar_status, "mode": cfg.mode, "broker": cfg.broker})
        if not calendar_status["trade_allowed"]:
            notifier.alert("Noon flat", f"{day} is {calendar_status['status']}. No trade.", toast=False)
            _write_report(cfg, out, day, calendar_status)
            return 0
        if cfg.broker == "ibkr":
            _assert_paper_accounts(broker)
        account = _resolve_account(cfg, broker)
        state = _State()
        saved = resume.load_open(cfg.state_dir, day)
        if saved:
            _load_saved(cfg, broker, state, saved)
            if state.position and _filled(_safe_state(broker, state.position.get("tp_order_id"))):
                prices = _prices_for(broker.option_quotes(), state.position)
                status = broker.order_state(state.position["tp_order_id"])
                _record_exit(cfg, broker, notifier, out, state, status["avg_price"], "target", prices)
                _write_report(cfg, out, day, calendar_status)
                return 0
        start = datetime.combine(day, cfg.t("start_not_before"), ET)
        if broker.now() < start:
            broker.sleep((start - broker.now()).total_seconds())
        closes = decision_closes(day, ET)
        grace = timedelta(seconds=cfg.decision_grace_s)
        while True:
            if state.position is not None:
                if _manage_position(cfg, broker, notifier, out, state):
                    break
                _sleep_for(cfg, broker, state, closes)
                continue
            if state.working is not None:
                _poll_working(cfg, broker, notifier, out, state)
                if state.position is not None or state.working is not None:
                    if state.position is None:
                        _sleep_for(cfg, broker, state, closes)
                    continue
            if state.halt or state.done_for_day:
                break
            if broker.now().time() >= datetime.strptime("15:10", "%H:%M").time() and state.position is None:
                break
            _maybe_enter(cfg, broker, notifier, approver, out, state, account, closes)
            if state.working is not None:
                _poll_working(cfg, broker, notifier, out, state)
            if state.halt and state.position is None:
                break
            if state.done_for_day and state.position is None and state.working is None:
                break
            if _window_finished(broker, state, closes, grace):
                break
            _sleep_for(cfg, broker, state, closes)
        _write_report(cfg, out, day, calendar_status)
        return 0
    finally:
        try:
            broker.disconnect()
        except Exception:  # noqa: BLE001
            log.warning("disconnect failed", exc_info=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spx_noon")
    parser.add_argument("--config", default=None)
    parser.add_argument("--mode", choices=["dry", "alerts", "paper", "live"], default=None)
    args = parser.parse_args(argv)
    overrides = {}
    if args.mode:
        overrides["mode"] = args.mode
    try:
        cfg = load_config(args.config, **overrides)
    except (SafetyError, ValueError, TypeError) as exc:
        print(f"refusing to start: {exc}")
        return 2
    _setup_logging(cfg)
    if cfg.broker == "ibkr":
        from .ib import IBBroker
        broker = IBBroker(cfg)
    elif cfg.broker == "schwab":
        from .schwab import SchwabBroker
        broker = SchwabBroker(cfg)
    else:
        print(f"refusing to start: no live broker for {cfg.broker}")
        return 2
    try:
        return run_session(cfg, broker)
    except SafetyError as exc:
        print(f"stopped: {exc}")
        return 2
