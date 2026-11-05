"""One noon session.

Waits for the 12:00–1:00 PM ET closes, takes the first qualifying bar, and
sends at most one spread. Paper on a Mac goes to TWS 7497. Tastytrade live
auto-approves. Schwab live still waits for `yes <ticketID>`.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .calendar import trading_day_status
from .config import SafetyError, load_config, resolve_broker
from .logbook import Outputs
from .notify import ConsoleApprover, Notifier
from .pricing import (
    entry_reprice_cap,
    floor_tick,
    market_data_class,
    next_entry_limit,
    quote_price_problem,
    reprice_quote_block,
    spread_prices,
    take_profit_limit,
    target_reached,
    time_exit_deadline,
    trade_pnl,
)
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
    accounts = [str(item) for item in (broker.managed_accounts() or [])]
    if cfg.broker == "tasty":
        return _tasty_account(cfg.account, accounts)
    if cfg.account:
        if accounts and cfg.account not in accounts and cfg.broker == "ibkr":
            raise SafetyError(f"account {cfg.account} is not managed by TWS ({accounts})")
        return cfg.account
    if not accounts:
        raise SafetyError("broker reported no account")
    return str(accounts[0])


def _tasty_account(want: str, accounts: list[str]) -> str:
    if not accounts:
        raise SafetyError("tastytrade reported no account")
    want = str(want or "").strip()
    if not want:
        if len(accounts) == 1:
            return accounts[0]
        raise SafetyError("tastytrade returned more than one account; set account to the one to use")
    exact = [acct for acct in accounts if acct == want]
    if len(exact) == 1:
        return exact[0]
    suffix = [acct for acct in accounts if acct.endswith(want)]
    if len(suffix) == 1:
        return suffix[0]
    raise SafetyError(f"account {want} does not match exactly one tastytrade account")


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


def _fill_price(state: dict | None) -> float | None:
    """Average price when the broker reports a positive fill. Zero is not a price."""
    if not state:
        return None
    try:
        filled = float(state.get("filled") or 0)
        avg = float(state.get("avg_price") or 0)
    except (TypeError, ValueError):
        return None
    if filled > 0 and avg > 0:
        return avg
    return None


def _remember_fill(working: dict, state: dict | None) -> None:
    price = _fill_price(state)
    if price is None:
        return
    working["seen_fill"] = price
    try:
        working["seen_qty"] = float(state.get("filled") or 0)
    except (TypeError, ValueError):
        working["seen_qty"] = 0.0


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


QUOTE_MAX_AGE_S = 30.0


def _quote_stale(quote, now: datetime, max_age: float = QUOTE_MAX_AGE_S) -> bool:
    ts = getattr(quote, "time", None)
    if ts is None:
        return False
    return (now - ts).total_seconds() > max_age


def _plan_quotes_stale(quotes, plan, now: datetime) -> bool:
    needed = (
        (plan.long_strike, plan.right),
        (plan.short_strike, plan.right),
        (plan.atm, "C"),
        (plan.atm, "P"),
    )
    for strike, right in needed:
        quote = lookup_quote(quotes, strike, right)
        if quote is not None and _quote_stale(quote, now):
            return True
    return False


def _still_in_grace(close_at: datetime, now: datetime, grace: timedelta) -> bool:
    return now < close_at + grace - timedelta(seconds=1)


def _await_terminal(broker, order_id, seconds: float):
    """Poll until the order is filled or done, or the wait runs out."""
    status = _safe_state(broker, order_id)
    if seconds <= 0 or status is None or status.get("done") or _filled(status):
        return status
    deadline = broker.now() + timedelta(seconds=seconds)
    while status and not status.get("done") and not _filled(status) and broker.now() < deadline:
        broker.sleep(1)
        status = _safe_state(broker, order_id)
    return status


def _ensure_position_quotes(cfg, broker, pos: dict) -> None:
    entry = pos.get("entry_close")
    if isinstance(entry, datetime):
        expiry = entry.strftime("%Y%m%d")
    else:
        expiry = str(pos.get("date") or "").replace("-", "")
    if not expiry:
        return
    spot = float(pos.get("spx") or pos["long_strike"])
    try:
        _subscribe(cfg, broker, spot, expiry)
        broker.ensure_options(expiry, [float(pos["long_strike"]), float(pos["short_strike"])])
    except Exception as exc:  # noqa: BLE001
        log.warning("could not subscribe the open spread: %s", exc)


def _ticket_text(plan, account: str = "", *, broker: str = "") -> str:
    if broker == "tasty":
        tail = "No stop. Tastytrade live auto-approves this ticket. The price cap is the mid limit."
    else:
        tail = "No stop. Live send still needs yes <ticket>."
    lines = [
        f"{plan.direction} debit vertical  1 spread",
        f"SPX {plan.spot:.2f}   PDH {plan.pdh:.2f}   PDL {plan.pdl:.2f}",
        f"expected move {plan.em:.2f}   width {plan.width}",
        f"long {plan.long_strike:.0f}{plan.right} Δ {plan.long_delta:.3f}",
        f"short {plan.short_strike:.0f}{plan.right} Δ {plan.short_delta if plan.short_delta is not None else float('nan'):.3f}",
        f"mid {plan.mid:.2f}   natural {plan.natural:.2f}   slippage {plan.slippage_per_side:.3f}/side",
        f"limit {plan.limit:.2f}   target {plan.limit * 1.30:.2f} then flat at {time_exit_deadline(plan.entry_close):%H:%M} ET",
        tail,
    ]
    if account:
        lines.append(f"account ending {str(account)[-3:]}")
    return "\n".join(lines)


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
    payload = {key: value for key, value in pos.items() if key != "plan"}
    for key in ("entry_close", "deadline", "cancel_after", "placed_at", "last_reprice_at"):
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


def _rest_target(cfg, broker, pos: dict) -> None:
    """Park the GTC take-profit. If the broker rejects it, the session still sells when the bid gets there."""
    if not cfg.sends_orders() or pos.get("tp_order_id") or broker.now() >= pos["deadline"]:
        return
    limit = float(pos["tp_limit"])
    try:
        order_id = broker.place_combo(
            pos["legs"], "SELL", 1, limit, pos["account"], str(pos["ref"]) + "X", tif="GTC")
    except Exception:  # noqa: BLE001
        log.exception("take-profit order was not accepted; watching the bid instead")
        return
    pos["tp_order_id"] = order_id
    try:
        _persist(cfg, pos)
    except Exception:  # noqa: BLE001
        log.exception("take-profit %s is working but was not saved", order_id)


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
        had_fill=True,
    )
    state.working = None
    state.done_for_day = True
    state.position = pos
    _persist(cfg, pos)
    _ensure_position_quotes(cfg, broker, pos)
    _rest_target(cfg, broker, pos)
    parked = (
        f"GTC close at {tp_limit:.2f}"
        if pos.get("tp_order_id")
        else f"watching the bid for {tp_limit:.2f}"
    )
    notifier.alert(
        "Noon filled",
        f"{pos['direction']} filled {fill:.2f} (mid was {plan.mid:.2f}). "
        f"{parked}. Flat at {pos['deadline']:%H:%M} ET. No stop.",
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
    if int(pos.get("exit_attempts") or 0) >= 3:
        if not pos.get("flat_gave_up"):
            pos["flat_gave_up"] = True
            notifier.alert(
                "Noon flatten stuck",
                "three exit attempts failed; no further sells. Close the spread by hand.",
                toast=True,
            )
            try:
                _persist(cfg, pos)
            except Exception:  # noqa: BLE001
                log.exception("could not save the flatten stop")
        return False
    existing = pos.get("exit_order_id")
    if existing:
        status = _await_terminal(broker, existing, 0)
        if _filled(status):
            _record_exit(cfg, broker, notifier, out, state, status["avg_price"], reason, prices)
            return True
        if status and not status.get("done"):
            log.info("flatten %s is still working; not sending another sell", existing)
            return False
    bid = None if prices is None else prices.get("close_bid")
    if bid is None or bid <= 0:
        if broker.now() < pos["deadline"] + timedelta(seconds=cfg.exit_wait_s):
            log.info("waiting for a positive spread bid to flatten")
            return False
        if not pos.get("flat_alerted"):
            pos["flat_alerted"] = True
            notifier.alert("Noon flatten", "no positive bid at the deadline; one last 0.05 sell", toast=True)
        limit = 0.05
    else:
        limit = max(0.05, floor_tick(bid))
    pos["exit_attempts"] = int(pos.get("exit_attempts") or 0) + 1
    try:
        order_id = broker.place_combo(
            pos["legs"], "SELL", 1, limit, pos["account"], pos["ref"] + "F", tif="DAY")
    except Exception:  # noqa: BLE001
        log.exception("flatten order was not accepted")
        return False
    pos["exit_order_id"] = order_id
    try:
        _persist(cfg, pos)
    except Exception:  # noqa: BLE001
        log.exception("flatten %s is working but was not saved", order_id)
    status = _await_terminal(broker, order_id, cfg.exit_wait_s)
    if _filled(status):
        fresh = _prices_for(broker.option_quotes(), pos)
        _record_exit(cfg, broker, notifier, out, state, status["avg_price"], reason, fresh or prices)
        return True
    if not (status and status.get("done")):
        try:
            broker.cancel(order_id)
        except Exception:  # noqa: BLE001
            log.warning("cancel flatten failed", exc_info=True)
        status = _await_terminal(broker, order_id, cfg.exit_wait_s)
        if _filled(status):
            _record_exit(cfg, broker, notifier, out, state, status["avg_price"], reason, prices)
            return True
        if status and not status.get("done"):
            return False
    log.warning("flatten order %s did not fill at %.2f", order_id, limit)
    pos["exit_order_id"] = None
    return False


def _manage_position(cfg, broker, notifier, out, state: _State) -> bool:
    """Return True when the spread is flat."""
    pos = state.position
    now = broker.now()
    _ensure_position_quotes(cfg, broker, pos)
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
            tp_state = _await_terminal(broker, tp_id, cfg.exit_wait_s)
            if _filled(tp_state):
                _record_exit(cfg, broker, notifier, out, state, tp_state["avg_price"], "target", prices)
                return True
            if tp_state and not tp_state.get("done"):
                log.warning("target %s still working; not sending another sell", tp_id)
                return False
        return _flatten(cfg, broker, notifier, out, state, reason, prices)
    return False


def _describe_quote(quote) -> str:
    """Data class plus prices. The receive stamp is not reported as an exchange time."""
    if quote is None:
        return "missing"
    kind = market_data_class(getattr(quote, "mdtype", None))
    problem = quote_price_problem(quote)
    stamp = "receive-stamped" if getattr(quote, "time", None) is not None else "no-receive-stamp"
    text = f"{kind} {stamp} bid {getattr(quote, 'bid', None)} ask {getattr(quote, 'ask', None)}"
    if problem:
        text += f" ({problem})"
    return text


def _log_entry_book(broker, plan) -> None:
    try:
        quotes = broker.option_quotes()
    except Exception:  # noqa: BLE001
        log.warning("entry quote diagnostic failed", exc_info=True)
        return
    long_q = lookup_quote(quotes, plan.long_strike, plan.right)
    short_q = lookup_quote(quotes, plan.short_strike, plan.right)
    log.info("entry quotes long %s short %s", _describe_quote(long_q), _describe_quote(short_q))


def _note_reprice(working: dict, reason: str) -> None:
    if working.get("reprice_note") == reason:
        return
    working["reprice_note"] = reason
    log.info("entry reprice %s held: %s", working.get("ref") or working.get("id"), reason)


def _entry_fill_refs(broker) -> set[str]:
    finder = getattr(broker, "own_entry_refs", None)
    if not callable(finder):
        return set()
    try:
        return {str(ref) for ref in (finder() or set()) if ref}
    except Exception:  # noqa: BLE001
        log.warning("entry fill lookup failed", exc_info=True)
        return set()


def _claim_existing_entry(cfg, broker, state: _State) -> bool:
    """Adopt one working Noon entry so a second order is not sent. True when placement must stop."""
    try:
        orders = broker.own_open_orders() or []
    except Exception:  # noqa: BLE001
        log.warning("could not list open orders; not sending a new entry yet", exc_info=True)
        return True
    buys = [
        order for order in orders
        if str(order.get("action") or "") == "BUY"
        and str(order.get("ref") or "").startswith("SPXNOON-")
        and not str(order.get("ref") or "").endswith(("X", "F"))
    ]
    if not buys:
        return False
    order = buys[0]
    limit = order.get("limit")
    log.warning("entry %s is already working at %s; not sending another", order.get("ref"), limit)
    state.working = {
        "id": order.get("order_id"),
        "entry_order_id": order.get("order_id"),
        "ref": order.get("ref"),
        "limit": limit,
        "original_limit": limit,
        "reprice_cap": limit,
        "reprice_attempts": int(cfg.entry_reprice_attempts),
        "reprice_stopped": "adopted",
        "status": "working",
        "account": order.get("account"),
        "cancel_after": broker.now() + timedelta(seconds=cfg.decision_grace_s),
        "plan": None,
    }
    try:
        _persist(cfg, state.working)
    except Exception:  # noqa: BLE001
        log.warning("could not save the adopted entry", exc_info=True)
    return True


def _open_known_fill(cfg, broker, notifier, state: _State, working: dict, price: float) -> None:
    if working.get("plan") is None:
        log.warning(
            "entry %s filled at %.2f but this process has no spread plan; not sending another order",
            working.get("id"), price,
        )
        state.done_for_day = True
        state.halt = True
        state.working = None
        return
    _open_position(cfg, broker, notifier, state, working, price)


def _maybe_reprice(cfg, broker, working: dict, status: dict | None) -> None:
    """Raise one working entry toward the current mid, inside the original cap."""
    if not cfg.entry_reprice_on() or working.get("reprice_stopped") or working.get("plan") is None:
        return
    if status is None:
        _note_reprice(working, "order status unknown")
        return
    if status.get("status") in ("PendingCancel", "Cancelled", "ApiCancelled", "Inactive", "Filled"):
        return
    if _fill_price(status) is not None:
        return
    cap = working.get("reprice_cap")
    current = working.get("limit")
    if cap is None or current is None:
        _note_reprice(working, "no original cap")
        return
    attempts = int(working.get("reprice_attempts") or 0)
    if attempts >= int(cfg.entry_reprice_attempts):
        _note_reprice(working, "attempt limit")
        return
    if float(current) + 1e-9 >= float(cap):
        _note_reprice(working, f"cap {float(cap):.2f}")
        return
    now = broker.now()
    anchor = working.get("last_reprice_at") or working.get("placed_at")
    if not isinstance(anchor, datetime):
        working["placed_at"] = now
        return
    if (now - anchor).total_seconds() + 1e-9 < float(cfg.entry_reprice_secs):
        return
    plan = working["plan"]
    try:
        quotes = broker.option_quotes()
    except Exception:  # noqa: BLE001
        log.warning("entry reprice quotes failed", exc_info=True)
        _note_reprice(working, "quotes unavailable")
        return
    long_q = lookup_quote(quotes, plan.long_strike, plan.right)
    short_q = lookup_quote(quotes, plan.short_strike, plan.right)
    block = reprice_quote_block(long_q, short_q, now, QUOTE_MAX_AGE_S)
    if block:
        _note_reprice(working, block)
        return
    prices = spread_prices(long_q, short_q)
    new = next_entry_limit(float(current), float(cap), prices["mid"], prices["natural"])
    if new is None:
        _note_reprice(working, f"mid {prices['mid']:.2f} natural {prices['natural']:.2f} at the limit")
        return
    try:
        broker.modify_combo(working["id"], new)
    except Exception:  # noqa: BLE001
        working["reprice_attempts"] = attempts + 1
        working["last_reprice_at"] = now
        log.warning(
            "entry reprice %s to %.2f failed (attempt %s/%s, cap %.2f)",
            working.get("ref") or working.get("id"), new, attempts + 1,
            cfg.entry_reprice_attempts, float(cap), exc_info=True,
        )
        try:
            _persist(cfg, working)
        except Exception:  # noqa: BLE001
            log.warning("could not save the failed reprice", exc_info=True)
        return
    working["limit"] = new
    working["reprice_attempts"] = attempts + 1
    working["last_reprice_at"] = now
    working["reprice_note"] = ""
    log.info(
        "entry reprice %s %.2f -> %.2f cap %.2f attempt %s/%s mid %.2f natural %.2f "
        "original %.2f status %s long %s short %s",
        working.get("ref"), float(current), new, float(cap), attempts + 1,
        cfg.entry_reprice_attempts, prices["mid"], prices["natural"],
        float(working.get("original_limit") or current), status.get("status"),
        _describe_quote(long_q), _describe_quote(short_q),
    )
    try:
        _persist(cfg, working)
    except Exception:  # noqa: BLE001
        log.warning("repriced entry %s was not saved", working.get("id"), exc_info=True)


def _finish_entry_cancel(cfg, broker, notifier, out, state: _State, working: dict) -> None:
    working["reprice_stopped"] = "cancel"
    try:
        broker.cancel(working["id"])
    except Exception:  # noqa: BLE001
        log.warning("cancel entry failed", exc_info=True)
    status = _await_terminal(broker, working["id"], cfg.exit_wait_s)
    _remember_fill(working, status)
    price = _fill_price(status)
    if price is not None:
        _open_known_fill(cfg, broker, notifier, state, working, price)
        return
    if working.get("seen_fill") and (status is None or not status.get("done")):
        log.warning("entry %s showed a fill and the cancel is unconfirmed; keeping it", working.get("id"))
        working["cancel_after"] = broker.now() + timedelta(seconds=max(cfg.exit_wait_s, 5))
        return
    if status is None or not status.get("done"):
        working["cancel_after"] = broker.now() + timedelta(seconds=max(cfg.exit_wait_s, 5))
        log.warning("entry %s still working after cancel; keeping it", working["id"])
        return
    if working.get("seen_fill"):
        log.warning(
            "entry %s showed a fill then a flat cancel; blocking another entry", working.get("id"),
        )
        state.halt = True
        state.done_for_day = True
        state.working = None
        return
    if working.get("plan") is None:
        log.warning("cancelled adopted entry %s; not sending another order", working.get("id"))
        state.halt = True
        state.working = None
        return
    out.append_trade(_row_from_plan(working["plan"], cfg.mode, "skip_no_fill"))
    notifier.alert(
        "Noon no fill",
        f"entry {working['ref']} cancelled unfilled at {float(working.get('limit') or 0):.2f}. "
        f"Reprice cap {float(working.get('reprice_cap') or working.get('limit') or 0):.2f}.",
        toast=False,
    )
    state.working = None
    resume.mark_closed(cfg.state_dir)


def _settle_partial(cfg, broker, notifier, state: _State, working: dict) -> None:
    """A partial entry is a position. Cancel the remainder and do not place another order."""
    working["reprice_stopped"] = "partial"
    if not working.get("partial_cancel_sent"):
        log.warning("entry %s partial fill; cancelling the remainder", working.get("id"))
        try:
            broker.cancel(working["id"])
        except Exception:  # noqa: BLE001
            log.warning("cancel partial entry failed", exc_info=True)
        working["partial_cancel_sent"] = True
        status = _await_terminal(broker, working["id"], cfg.exit_wait_s)
    else:
        status = _safe_state(broker, working["id"])
    _remember_fill(working, status)
    price = _fill_price(status) or working.get("seen_fill")
    if price and status and status.get("done"):
        _open_known_fill(cfg, broker, notifier, state, working, float(price))
        return
    if price and _filled(status):
        _open_known_fill(cfg, broker, notifier, state, working, float(price))
        return
    log.warning("entry %s partial fill is not terminal; not sending another order", working.get("id"))
    working["cancel_after"] = max(
        working.get("cancel_after") or broker.now(),
        broker.now() + timedelta(seconds=max(cfg.exit_wait_s, 5)),
    )


def _poll_working(cfg, broker, notifier, out, state: _State) -> None:
    working = state.working
    status = _safe_state(broker, working["id"])
    _remember_fill(working, status)
    if _filled(status):
        _open_known_fill(cfg, broker, notifier, state, working, status["avg_price"])
        return
    if _fill_price(status) is not None and not (status and status.get("done")):
        _settle_partial(cfg, broker, notifier, state, working)
        return
    if broker.now() >= working["cancel_after"]:
        _finish_entry_cancel(cfg, broker, notifier, out, state, working)
        return
    if status is None:
        if not working.get("unreconciled_logged"):
            log.warning("entry %s status unknown; not replacing it", working.get("id"))
            working["unreconciled_logged"] = True
        working["reprice_stopped"] = working.get("reprice_stopped") or "unknown"
        return
    _maybe_reprice(cfg, broker, working, status)


def _revalidate(cfg, broker, breakout, spot: float, expiry: str):
    """Rebuild the plan from fresh quotes after the operator approves."""
    try:
        _subscribe(cfg, broker, spot, expiry)
        quotes = broker.option_quotes()
    except Exception as exc:  # noqa: BLE001
        log.warning("revalidate quotes failed: %s", exc)
        return None
    fresh_spot = broker.spx_px().value or spot
    plan, _reason = build_order_plan(breakout, quotes, float(fresh_spot))
    if plan is None or _plan_quotes_stale(quotes, plan, broker.now()):
        return None
    return plan


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
        stale = plan is not None and _plan_quotes_stale(quotes, plan, broker.now())
        if plan is None or stale:
            if _still_in_grace(close_at, broker.now(), grace):
                log.info("waiting on quotes for %s (%s)", close_at.strftime("%H:%M"), reason or "stale")
                return
            state.consumed.add(close_at)
            log.info("giving up on %s: %s", close_at.strftime("%H:%M"), reason or "stale")
            continue
        state.consumed.add(close_at)
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
                _ticket_text(plan, broker=cfg.broker),
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
            notifier.alert("Noon ticket " + ticket, _ticket_text(plan, account, broker=cfg.broker))
            if not preview.get("ok", False):
                out.append_trade(_row_from_plan(plan, cfg.mode, "skip_preview"))
                continue
            if cfg.confirms_orders:
                approved, why = approver.ask(ticket, cfg.approval_timeout_secs)
                if not approved:
                    out.append_trade(_row_from_plan(plan, cfg.mode, "skip_approval"))
                    notifier.alert("Noon skipped", f"{ticket} {why}", toast=False)
                    continue
            if broker.now() > close_at + grace:
                out.append_trade(_row_from_plan(plan, cfg.mode, "skip_stale"))
                notifier.alert("Noon skipped", f"{ticket} decision window closed", toast=False)
                continue
            fresh = _revalidate(cfg, broker, breakout, float(spot), expiry)
            if fresh is None or fresh.skip:
                out.append_trade(_row_from_plan(plan, cfg.mode, "skip_stale"))
                notifier.alert("Noon skipped", f"{ticket} quotes moved or went stale", toast=False)
                continue
            plan = fresh
            broker.approve_ticket(ticket, plan.limit)
        if _claim_existing_entry(cfg, broker, state):
            if state.working is None and _still_in_grace(close_at, broker.now(), grace):
                state.consumed.discard(close_at)
            return
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
        cap = plan.limit
        if cfg.entry_reprice_on():
            cap = entry_reprice_cap(plan.limit, plan.natural, cfg.entry_reprice_max_add)
        working = _pos_from_plan(plan, None, status="working", account=account, ref=ref, ticket=ticket,
                                 entry_order_id=order_id, mode=cfg.mode)
        working["id"] = order_id
        working["plan"] = plan
        working["cancel_after"] = _next_decision(close_at, grace)
        working["original_limit"] = plan.limit
        working["original_natural"] = plan.natural
        working["reprice_cap"] = cap
        working["reprice_attempts"] = 0
        working["placed_at"] = broker.now()
        state.working = working
        _persist(cfg, working)
        _log_entry_book(broker, plan)
        log.info(
            "working entry %s limit %.2f mid %.2f natural %.2f reprice_cap %.2f paper_reprice %s",
            ref, plan.limit, plan.mid, plan.natural, cap, cfg.entry_reprice_on(),
        )
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
    for key in ("placed_at", "last_reprice_at"):
        if pos.get(key):
            pos[key] = _restore_dt(pos[key])
    if pos.get("status") == "working":
        order_id = pos.get("entry_order_id") or pos.get("id")
        if pos.get("reprice_cap") is None:
            # An order written before repricing has no room to pay up.
            pos["reprice_cap"] = pos.get("original_limit") or pos.get("limit")
        status = _safe_state(broker, order_id)
        notifier = Notifier(cfg.home / "logs", toast=False)
        if _filled(status):
            pos["id"] = order_id
            pos["plan"] = _plan_shim(pos)
            _open_position(cfg, broker, notifier, state, pos, status["avg_price"])
            return
        if _fill_price(status) is not None:
            pos["id"] = order_id
            pos["plan"] = _plan_shim(pos)
            pos["seen_fill"] = _fill_price(status)
            state.working = pos
            return
        if status and not status.get("done"):
            pos["id"] = order_id
            pos["plan"] = _plan_shim(pos)
            state.working = pos
            return
        if pos.get("ref") and pos.get("ref") in _entry_fill_refs(broker) and pos.get("direction"):
            log.warning(
                "restart found a fill report for %s but no combo price; "
                "not sending another entry and not inventing a take-profit",
                pos.get("ref"),
            )
            state.done_for_day = True
            state.halt = True
            return
        if status and status.get("done"):
            resume.mark_closed(cfg.state_dir)
            return
        log.warning("restart could not reconcile entry %s; no new order until it is resolved", order_id)
        pos["id"] = order_id
        pos["plan"] = _plan_shim(pos) if pos.get("direction") else None
        pos["reprice_stopped"] = "unreconciled"
        state.working = pos
        return
    if pos.get("fill_price"):
        pos["had_fill"] = True
    state.done_for_day = True
    state.position = pos
    _ensure_position_quotes(cfg, broker, pos)
    if pos.get("tp_order_id") and _filled(_safe_state(broker, pos["tp_order_id"])):
        return
    if not pos.get("tp_order_id") and broker.now() < pos["deadline"]:
        pos["tp_limit"] = pos.get("tp_limit") or take_profit_limit(pos["fill_price"])
        _rest_target(cfg, broker, pos)


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
    lines.append(
        "Version B: 75-delta debit, limit at mid, +30% target, flat two hours after the entry bar. "
        "IBKR paper may raise a working entry by at most $0.10 over that original limit."
    )
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
        starter = getattr(broker, "start_streams", None)
        if callable(starter):
            try:
                starter()
            except Exception:  # noqa: BLE001
                log.exception("quote stream did not start")
                return 1
        account = _resolve_account(cfg, broker)
        state = _State()
        if resume.already_filled(cfg.state_dir, day):
            log.info("already filled today; no second entry")
            state.done_for_day = True
        saved = resume.load_open(cfg.state_dir, day)
        if saved:
            _load_saved(cfg, broker, state, saved)
            if state.position and _filled(_safe_state(broker, state.position.get("tp_order_id"))):
                prices = _prices_for(broker.option_quotes(), state.position)
                status = broker.order_state(state.position["tp_order_id"])
                _record_exit(cfg, broker, notifier, out, state, status["avg_price"], "target", prices)
                _write_report(cfg, out, day, calendar_status)
                return 0
        if state.working is None and state.position is None and not state.done_for_day:
            refs = _entry_fill_refs(broker)
            if refs:
                log.warning(
                    "entry fill %s is already on the broker; not sending another order",
                    sorted(refs)[0],
                )
                state.done_for_day = True
            else:
                _claim_existing_entry(cfg, broker, state)
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
    parser.add_argument("--broker", default="auto",
                        help="auto, tasty, tt, tastytrade, ibkr, or schwab. auto is tastytrade on Windows.")
    args = parser.parse_args(argv)
    overrides = {}
    if args.mode:
        overrides["mode"] = args.mode
    try:
        overrides["broker"] = resolve_broker(args.broker)
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
    elif cfg.broker == "tasty":
        from .tasty import TastyBroker
        broker = TastyBroker(cfg)
    else:
        print(f"refusing to start: no live broker for {cfg.broker}")
        return 2
    try:
        return run_session(cfg, broker)
    except SafetyError as exc:
        print(f"stopped: {exc}")
        return 2
