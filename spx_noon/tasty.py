"""TastyBroker: quotes, bars, the SPXW chain, greeks, and orders.

Market data and orders are separate sessions. The data session is production
unless ``tasty_data`` says sandbox. Order calls take the order session only.
Sandbox orders load sandbox credentials and must use api.cert.tastyworks.com.
Passing the data session into place, replace, or cancel aborts.

Order ids land in ``state/tt_orders-YYYY-MM-DD.json``. Secrets are never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import guard
from .broker import Broker
from .config import ORDER_PREFIX, SafetyError
from .pricing import floor_tick
from .strategy import Bar, OptQuote, Px

ET = ZoneInfo("America/New_York")
log = logging.getLogger("spx_noon.tasty")

CERT_HOST = "api.cert.tastyworks.com"
PROD_HOST = "api.tastyworks.com"
INDEX = "SPX"
VIX = "VIX"

_SECRET_FILES = {
    "sandbox": ("TASTYTRADE_SANDBOX_CLIENT_SECRET", "TASTYTRADE_SANDBOX_REFRESH_TOKEN",
                ".sb_secret", ".sb_refresh"),
    "production": ("TASTYTRADE_PROD_CLIENT_SECRET", "TASTYTRADE_PROD_REFRESH_TOKEN",
                   ".prod_secret", ".prod_refresh"),
}

TERMINAL = {"Filled", "Cancelled", "Expired", "Rejected", "Removed", "Partially Removed"}
FILLED = {"Filled"}
CANCELLED = {"Cancelled", "Expired", "Rejected", "Removed", "Partially Removed"}


class _SecretFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        redacted = message
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "[redacted]")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def cred_dir() -> Path:
    raw = os.environ.get("TASTYTRADE_CRED_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "secrets"


def load_creds(kind: str, directory: Path | None = None) -> tuple[str, str]:
    """Read one environment's secret and refresh token. Never log the values.

    Files are ``<cred dir>/.sb_secret`` and the matching refresh file, or the
    production pair. The directory is ``TASTYTRADE_CRED_DIR`` or ``~/secrets``.
    """
    spec = _SECRET_FILES.get(kind)
    if spec is None:
        raise SafetyError(f"unknown tastytrade environment {kind!r}")
    env_secret, env_token, file_secret, file_token = spec
    folder = directory if directory is not None else cred_dir()
    secret = os.environ.get(env_secret, "").strip()
    token = os.environ.get(env_token, "").strip()
    if not secret:
        secret = _read_secret(folder / file_secret)
    if not token:
        token = _read_secret(folder / file_token)
    if not secret or not token:
        raise SafetyError(
            f"missing tastytrade {kind} credentials "
            f"({env_secret} and {env_token}, or {folder / file_secret} and {folder / file_token})"
        )
    return secret, token


def _read_secret(path: Path) -> str:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SafetyError(f"could not read {path.name} ({exc.__class__.__name__})") from None
    return ""


def host_for(mode: str) -> str:
    if mode == "sandbox":
        return CERT_HOST
    if mode == "production":
        return PROD_HOST
    raise SafetyError(f"unknown tastytrade environment {mode!r}")


def session_host(session) -> str:
    """Hostname the session will actually call.

    A label on the object does not count when the HTTP client has a base URL.
    """
    client = getattr(session, "_client", None)
    if client is not None:
        base = getattr(client, "base_url", None)
        host = getattr(base, "host", None) if base is not None else None
        if host:
            return str(host)
        text = str(base or "")
        for known in (CERT_HOST, PROD_HOST):
            if known in text:
                return known
        if text:
            return text
    host = getattr(session, "host", None)
    if not host:
        raise SafetyError("session has no API host")
    return str(host)


def _hostname(host: str) -> str:
    text = str(host).replace("https://", "").replace("http://", "").strip()
    return text.split("/")[0]


def safety_banner(data_host: str, order_host: str, orders_mode: str) -> str:
    real = "YES" if orders_mode == "production" else "NO"
    return f"DATA={_hostname(data_host)} ORDERS={_hostname(order_host)} REAL MONEY={real}"


def _finite(value):
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _as_et(moment) -> datetime | None:
    if moment is None:
        return None
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(ET)
    return None


def _ms_to_et(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).astimezone(ET)
    except (TypeError, ValueError, OSError):
        return None


def candles_to_bars(events) -> list[Bar]:
    """DXFeed candle time is the bar start, which is what the session expects."""
    out = []
    for event in events or []:
        stamp = _ms_to_et(getattr(event, "time", None))
        if stamp is None:
            continue
        out.append(Bar(
            stamp,
            float(event.open),
            float(event.high),
            float(event.low),
            float(event.close),
            float(getattr(event, "volume", 0) or 0),
        ))
    out.sort(key=lambda bar: bar.t)
    return [bar for bar in out if time(9, 30) <= bar.t.time() < time(16, 0)]


def build_vertical_order(legs, action: str, qty: int, limit: float, tif: str = "DAY",
                         external_id: str | None = None) -> dict:
    """Debit buys are a negative price. Credit sells are positive.

    ``legs`` are ``(occ_symbol, ratio)`` with +1 long and −1 short on the open.
    """
    if int(qty) != 1:
        raise guard.GuardError(f"noon breakout sends 1 spread, not {qty}")
    if float(limit) <= 0:
        raise guard.GuardError(f"refusing non-positive limit {limit}")
    price = floor_tick(float(limit))
    if price <= 0:
        raise guard.GuardError(f"refusing non-positive limit {limit}")
    opening = action == "BUY"
    order_legs = []
    for symbol, ratio in legs:
        buy_leg = (int(ratio) > 0) == opening
        side = f"{'Buy' if buy_leg else 'Sell'} to {'Open' if opening else 'Close'}"
        order_legs.append({
            "instrument-type": "Equity Option",
            "symbol": str(symbol),
            "action": side,
            "quantity": 1,
        })
    signed = -price if action == "BUY" else price
    payload = {
        "time-in-force": "GTC" if tif == "GTC" else "Day",
        "order-type": "Limit",
        "price": signed,
        "price-effect": "Debit" if action == "BUY" else "Credit",
        "legs": order_legs,
    }
    if external_id:
        payload["external-identifier"] = external_id
    return payload


def _status_name(status) -> str:
    return str(getattr(status, "value", status) or "")


def _fill_price(order, action: str, fallback: float) -> float:
    avg = None
    try:
        avg = _finite(order.average_fill_price())
    except Exception:  # noqa: BLE001
        avg = None
    if avg is None:
        avg = _finite(getattr(order, "price", None))
    if avg is None:
        return float(fallback)
    return round(abs(avg), 4)


class _Loop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="tasty-loop", daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout: float = 60):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


class TastyBroker(Broker):
    def __init__(self, cfg, *, data_session=None, order_session=None, session_factory=None,
                 transport=None, clock=_time.monotonic):
        cfg.validate()
        if cfg.broker != "tasty":
            raise SafetyError("TastyBroker is the tastytrade setup")
        self.cfg = cfg
        self.data_session = data_session
        self.order_session = order_session
        self.session_factory = session_factory
        self.transport = transport
        self.clock = clock
        self.guard = guard.OrderGuard(
            live=cfg.live, max_per_min=min(10, int(cfg.max_orders_per_min)),
            min_replace_secs=cfg.min_replace_secs, clock=clock)
        self.ticket: str | None = None
        self._loop: _Loop | None = None
        self._redact = None
        self._secrets: list[str] = []
        self._accounts: list[str] = []
        self._account_objs: dict = {}
        self._index: dict[str, Px] = {}
        self._index_at = -1e9
        self._strikes: dict[str, list[float] | None] = {}
        self._book: dict[tuple, dict] = {}
        self._quotes: dict[tuple, OptQuote] = {}
        self._quote_lock = threading.Lock()
        self._want: set = set()
        self._expiry: str | None = None
        self._feed_started = False
        self._stop = False
        self.today = datetime.now(ET).date()
        self.store_path = cfg.state_dir / f"tt_orders-{self.today.isoformat()}.json"
        self.store = {"tickets": {}, "orders": {}}
        if self.store_path.exists():
            try:
                self.store = json.loads(self.store_path.read_text())
            except ValueError:
                pass
        if self.data_session is not None and self.order_session is not None:
            self._assert_session_pair()

    def safety_banner(self) -> str:
        if self.data_session is None or self.order_session is None:
            return safety_banner(
                host_for(self.cfg.tasty_data), host_for(self.cfg.tasty_orders), self.cfg.tasty_orders)
        return safety_banner(
            session_host(self.data_session), session_host(self.order_session), self.cfg.tasty_orders)

    def _announce(self) -> str:
        line = self.safety_banner()
        log.info("%s", line)
        print(line, flush=True)
        return line

    def _arm_redaction(self, secrets) -> None:
        fresh = [s for s in secrets if s and s not in self._secrets]
        self._secrets.extend(fresh)
        if fresh and self._redact is None:
            self._redact = _SecretFilter(self._secrets)
            logging.getLogger().addFilter(self._redact)
            logging.getLogger("tastytrade").addFilter(self._redact)
        elif self._redact is not None:
            self._redact._secrets = [s for s in self._secrets if s and len(s) >= 6]

    def _open_sessions(self) -> None:
        """Load credentials and open two sessions. No fallback from production data."""
        data_mode = self.cfg.tasty_data
        order_mode = self.cfg.tasty_orders
        data_creds = load_creds(data_mode)
        order_creds = load_creds(order_mode)
        if order_mode == "sandbox" and data_mode != "sandbox" and data_creds == order_creds:
            raise SafetyError(
                "sandbox orders refused: order credentials match the production data credentials")
        self._arm_redaction((*data_creds, *order_creds))
        if self.session_factory is not None:
            self.data_session = self.session_factory(*data_creds, is_test=data_mode == "sandbox")
            self.order_session = self.session_factory(*order_creds, is_test=order_mode == "sandbox")
        else:
            self.data_session, self.order_session = self._call(self._open_pair(data_creds, order_creds))
        if self.data_session is self.order_session:
            self.data_session = None
            self.order_session = None
            raise SafetyError("abort: data session and order session are the same object")
        self._assert_session_pair()

    async def _open_pair(self, data_creds, order_creds):
        from tastytrade import Session
        data = Session(data_creds[0], data_creds[1], is_test=self.cfg.tasty_data == "sandbox")
        orders = Session(order_creds[0], order_creds[1], is_test=self.cfg.tasty_orders == "sandbox")
        await data._client.__aenter__()
        await orders._client.__aenter__()
        return data, orders

    def _assert_session_pair(self) -> None:
        if self.data_session is None or self.order_session is None:
            raise SafetyError("tastytrade sessions are not open")
        if self.data_session is self.order_session:
            raise SafetyError("abort: data session and order session are the same object")
        data_host = session_host(self.data_session)
        order_host = session_host(self.order_session)
        expect_data = host_for(self.cfg.tasty_data)
        expect_order = host_for(self.cfg.tasty_orders)
        if expect_data not in data_host:
            raise SafetyError(f"data session host {data_host} is not {expect_data}")
        if expect_order not in order_host:
            raise SafetyError(f"order session host {order_host} is not {expect_order}")
        if self.cfg.tasty_orders == "sandbox":
            if getattr(self.order_session, "is_test", False) is not True:
                raise SafetyError("sandbox order session is not a cert session")
            if CERT_HOST not in order_host:
                raise SafetyError(f"sandbox orders refused: host {order_host} is not {CERT_HOST}")
        if self.cfg.tasty_orders == "production" and getattr(self.order_session, "is_test", False):
            raise SafetyError("production order session is marked sandbox")

    def _assert_order_session(self, session) -> None:
        """place / replace / cancel. Sandbox must be the cert host. Data session aborts."""
        if session is None:
            raise SafetyError("abort: no order session")
        if self.data_session is not None and session is self.data_session:
            raise SafetyError("abort: data session passed to an order call")
        host = session_host(session)
        if self.cfg.tasty_orders == "sandbox":
            if CERT_HOST not in host:
                raise SafetyError(f"sandbox order refused: host {host} is not {CERT_HOST}")
            if getattr(session, "is_test", False) is not True and getattr(session, "_client", None) is None:
                raise SafetyError("sandbox order refused: session is not the cert session")
            if getattr(session, "is_test", True) is False:
                raise SafetyError("sandbox order refused: session is not the cert session")
        elif self.cfg.tasty_orders == "production":
            if CERT_HOST in host or getattr(session, "is_test", False):
                raise SafetyError(f"production order refused: host {host}")
            if PROD_HOST not in host:
                raise SafetyError(f"production order refused: host {host} is not {PROD_HOST}")
        else:
            raise SafetyError(f"tasty_orders {self.cfg.tasty_orders!r} is not usable")

    def connect(self):
        if self.data_session is None or self.order_session is None:
            self._open_sessions()
        self._assert_session_pair()
        self._announce()
        if self.cfg.live:
            for ticket_id, ticket in self.store.get("tickets", {}).items():
                if not ticket.get("finished"):
                    self.guard.approve(ticket_id, float(ticket["cap"]))
                    self.ticket = ticket_id
            for order_id, order in self.store.get("orders", {}).items():
                self.guard.bind(order.get("current") or order_id, order["ticket"])

    def ensure_connected(self) -> bool:
        try:
            if self.data_session is None or self.order_session is None:
                self.connect()
            self.spx_px()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("tastytrade unreachable: %s", exc.__class__.__name__)
            return False

    def disconnect(self):
        self._stop = True
        loop = self._loop
        if loop is None:
            return
        try:
            if self.data_session is not None or self.order_session is not None:
                self._call(self._aclose(), timeout=10)
        except Exception:  # noqa: BLE001
            log.warning("tastytrade disconnect failed", exc_info=False)
        loop.stop()
        self._loop = None

    async def _aclose(self):
        for session in (self.order_session, self.data_session):
            client = getattr(session, "_client", None)
            if client is not None:
                try:
                    await client.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

    def _redacted(self, exc: BaseException) -> str:
        text = f"{exc.__class__.__name__}: {exc}"
        for secret in self._secrets:
            if secret and len(secret) >= 6 and secret in text:
                text = text.replace(secret, "[redacted]")
        text = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", text)
        text = re.sub(
            r"((?:refresh_token|client_secret|access_token)\"?\s*[:=]\s*\"?)[^\s\",}]+",
            r"\1[redacted]",
            text,
            flags=re.IGNORECASE,
        )
        return text[:400]

    def _call(self, coro, timeout: float = 60):
        if self._loop is None:
            self._loop = _Loop()
        try:
            return self._loop.call(coro, timeout=timeout)
        except SafetyError:
            raise
        except Exception as exc:
            log.warning("tastytrade call failed (%s)", exc.__class__.__name__)
            raise SafetyError(self._redacted(exc)) from None

    def now(self):
        return datetime.now(ET)

    def sleep(self, seconds):
        _time.sleep(max(0.0, float(seconds)))

    def managed_accounts(self) -> list[str]:
        if self.transport is not None:
            self._accounts = [str(item) for item in getattr(self.transport, "accounts", []) or []]
            return list(self._accounts)
        rows = self._call(self._fetch_accounts())
        self._accounts = [str(row.account_number) for row in rows]
        self._account_objs = {str(row.account_number): row for row in rows}
        return list(self._accounts)

    async def _fetch_accounts(self):
        from tastytrade.account import Account
        self._assert_order_session(self.order_session)
        rows = await Account.get(self.order_session)
        return [row for row in rows if not getattr(row, "is_closed", False)]

    def net_liquidation(self, account: str):
        if self.transport is not None:
            return getattr(self.transport, "net_liq", None)
        try:
            return self._call(self._net_liq(str(account)))
        except Exception as exc:  # noqa: BLE001
            log.warning("balance lookup failed: %s", exc.__class__.__name__)
            return None

    async def _net_liq(self, account_number: str):
        self._assert_order_session(self.order_session)
        account = self._account_objs.get(account_number)
        if account is None:
            await self._fetch_accounts()
            account = self._account_objs.get(account_number)
        if account is None:
            return None
        balances = await account.get_balances(self.order_session)
        return _finite(getattr(balances, "net_liquidating_value", None))

    def spx_hours(self):
        if self.transport is not None and hasattr(self.transport, "spx_hours"):
            return self.transport.spx_hours()
        try:
            return self._call(self._hours())
        except Exception as exc:  # noqa: BLE001
            log.warning("market hours unavailable: %s", exc.__class__.__name__)
            return None, None

    async def _hours(self):
        from tastytrade.market_sessions import ExchangeType, get_market_sessions
        if self.data_session is self.order_session:
            raise SafetyError("abort: data session and order session are the same object")
        rows = await get_market_sessions(self.data_session, [ExchangeType.NYSE])
        day = datetime.now(ET)
        key = day.strftime("%Y%m%d")
        for row in rows or []:
            status = _status_name(getattr(row, "status", "")).lower()
            if status == "closed":
                return f"{key}:CLOSED", "America/New_York"
            opened = _as_et(getattr(row, "open_at", None))
            closed = _as_et(getattr(row, "close_at", None))
            if opened and closed:
                return f"{opened:%Y%m%d:%H%M}-{closed:%Y%m%d:%H%M}", "America/New_York"
        return None, None

    def _poll_index(self, symbols: tuple[str, ...] = (INDEX, VIX)):
        if self.transport is not None:
            return
        if self.clock() - self._index_at < self.cfg.poll_secs and all(sym in self._index for sym in symbols):
            return
        rows = self._call(self._index_quotes(symbols))
        now = datetime.now(ET)
        for row in rows or []:
            symbol = str(getattr(row, "symbol", "") or "")
            value = _finite(getattr(row, "last", None))
            if value is None:
                value = _finite(getattr(row, "mark", None)) or _finite(getattr(row, "close", None))
            stamp = _as_et(getattr(row, "updated_at", None)) or now
            high = _finite(getattr(row, "day_high_price", None)) or _finite(getattr(row, "day_high", None))
            self._index[symbol] = Px(value, stamp, 1, high)
        self._index_at = self.clock()

    async def _index_quotes(self, symbols):
        from tastytrade.market_data import get_market_data_by_type
        if self.data_session is self.order_session:
            raise SafetyError("abort: data session and order session are the same object")
        return await get_market_data_by_type(self.data_session, indices=list(symbols))

    def spx_px(self) -> Px:
        if self.transport is not None:
            return self.transport.spx_px()
        try:
            self._poll_index()
        except Exception as exc:  # noqa: BLE001
            log.warning("SPX quote failed: %s", exc.__class__.__name__)
        return self._index.get(INDEX) or Px(None, None)

    def vix_px(self) -> Px:
        if self.transport is not None and hasattr(self.transport, "vix_px"):
            return self.transport.vix_px()
        try:
            self._poll_index()
        except Exception as exc:  # noqa: BLE001
            log.warning("VIX quote failed: %s", exc.__class__.__name__)
        return self._index.get(VIX) or Px(None, None)

    def spx_bars_15m(self, days: int = 5) -> list[Bar]:
        return self._bars("15m", days)

    def spx_bars_1m(self, days: int = 1) -> list[Bar]:
        return self._bars("1m", days)

    def _bars(self, interval: str, days: int) -> list[Bar]:
        if self.transport is not None:
            getter = getattr(self.transport, "bars", None)
            if callable(getter):
                return list(getter(interval, days))
            return []
        start = datetime.now(ET) - timedelta(days=max(1, int(days)) + 4)
        events = self._call(self._collect_candles(interval, start), timeout=40)
        return candles_to_bars(events)

    async def _collect_candles(self, interval: str, start: datetime):
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Candle
        if self.data_session is self.order_session:
            raise SafetyError("abort: data session and order session are the same object")
        found = []
        async with DXLinkStreamer(self.data_session) as streamer:
            await streamer.subscribe_candle([INDEX], interval, start_time=start, extended_trading_hours=False)
            quiet = asyncio.get_running_loop().time()
            deadline = quiet + 20
            last = quiet
            while asyncio.get_running_loop().time() < deadline:
                event = streamer.get_event_nowait(Candle)
                if event is None:
                    if found and asyncio.get_running_loop().time() - last > 1.2:
                        break
                    await asyncio.sleep(0.15)
                    continue
                found.append(event)
                last = asyncio.get_running_loop().time()
                deadline = min(deadline, last + 8)
            try:
                await streamer.unsubscribe_candle(INDEX, interval)
            except Exception:  # noqa: BLE001
                pass
        return found

    def chain_strikes(self, expiry: str):
        if expiry in self._strikes:
            return self._strikes[expiry]
        if self.transport is not None:
            strikes = self.transport.chain_strikes(expiry)
            self._strikes[expiry] = strikes
            return strikes
        strikes, book = self._call(self._chain(expiry), timeout=45)
        self._book.update(book)
        self._strikes[expiry] = strikes
        return strikes

    async def _chain(self, expiry: str):
        from tastytrade.instruments import NestedOptionChain, Option
        if self.data_session is self.order_session:
            raise SafetyError("abort: data session and order session are the same object")
        chains = await NestedOptionChain.get(self.data_session, INDEX)
        day = date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))
        strikes = set()
        book = {}
        for chain in chains or []:
            root = str(getattr(chain, "root_symbol", "") or "")
            for exp in chain.expirations or []:
                if exp.expiration_date != day:
                    continue
                for strike in exp.strikes or []:
                    call = str(getattr(strike, "call", "") or "")
                    put = str(getattr(strike, "put", "") or "")
                    if root != "SPXW" and not call.startswith("SPXW"):
                        continue
                    k = round(float(strike.strike_price), 2)
                    strikes.add(k)
                    call_stream = str(getattr(strike, "call_streamer_symbol", "") or "")
                    put_stream = str(getattr(strike, "put_streamer_symbol", "") or "")
                    if call and not call_stream:
                        call_stream = Option.occ_to_streamer_symbol(_pad_occ(call))
                    if put and not put_stream:
                        put_stream = Option.occ_to_streamer_symbol(_pad_occ(put))
                    book[(k, "C")] = {"occ": call, "streamer": call_stream}
                    book[(k, "P")] = {"occ": put, "streamer": put_stream}
        found = sorted(strikes) or None
        return found, book

    def ensure_options(self, expiry, strikes):
        self._expiry = str(expiry)
        if self.transport is not None:
            ensure = getattr(self.transport, "ensure_options", None)
            if callable(ensure):
                ensure(expiry, strikes)
            return
        if expiry not in self._strikes:
            self.chain_strikes(expiry)
        spot = None
        cached = self._index.get(INDEX)
        if cached is not None:
            spot = cached.value
        want = {round(float(strike), 2) for strike in strikes}
        budget = max(1, int(self.cfg.max_option_lines) // 2)
        if spot and len(want) > budget:
            want = set(sorted(want, key=lambda strike: abs(strike - spot))[:budget])
        self._want = {(strike, right) for strike in want for right in ("C", "P")}
        self.start_streams()

    def start_streams(self):
        if self.transport is not None:
            starter = getattr(self.transport, "start_streams", None)
            if callable(starter):
                starter()
            return
        if self._feed_started or self.data_session is None:
            return
        self._feed_started = True
        self._stop = False

        async def _go():
            try:
                await self._feed()
            except Exception:  # noqa: BLE001
                log.warning("option stream stopped", exc_info=False)

        if self._loop is None:
            self._loop = _Loop()
        asyncio.run_coroutine_threadsafe(_go(), self._loop.loop)

    async def _feed(self):
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Greeks, Quote
        if self.data_session is self.order_session:
            raise SafetyError("abort: data session and order session are the same object")
        async with DXLinkStreamer(self.data_session) as streamer:
            self._streamer = streamer
            await streamer.subscribe(Quote, [INDEX, VIX])
            subscribed: set[str] = set()
            while not self._stop:
                pending = []
                for key in list(self._want):
                    meta = self._book.get(key) or {}
                    symbol = meta.get("streamer")
                    if symbol and symbol not in subscribed:
                        pending.append(symbol)
                        subscribed.add(symbol)
                if pending:
                    await streamer.subscribe(Quote, pending)
                    await streamer.subscribe(Greeks, pending)
                self._drain(streamer, Quote, Greeks)
                await asyncio.sleep(0.2)
            self._streamer = None

    def _drain(self, streamer, quote_cls, greeks_cls):
        quotes = {}
        greeks = {}
        while True:
            event = streamer.get_event_nowait(quote_cls)
            if event is None:
                break
            quotes[event.event_symbol] = event
        while True:
            event = streamer.get_event_nowait(greeks_cls)
            if event is None:
                break
            greeks[event.event_symbol] = event
        if not quotes and not greeks:
            return
        with self._quote_lock:
            for key, meta in self._book.items():
                symbol = meta.get("streamer")
                if not symbol:
                    continue
                if symbol not in quotes and symbol not in greeks and key in self._quotes:
                    continue
                quote = quotes.get(symbol)
                greek = greeks.get(symbol)
                prev = self._quotes.get(key)
                bid = _finite(getattr(quote, "bid_price", None)) if quote is not None else None
                ask = _finite(getattr(quote, "ask_price", None)) if quote is not None else None
                if bid is None and prev is not None:
                    bid = prev.bid
                if ask is None and prev is not None:
                    ask = prev.ask
                delta = _finite(getattr(greek, "delta", None)) if greek is not None else None
                iv = _finite(getattr(greek, "volatility", None)) if greek is not None else None
                if delta is None and prev is not None:
                    delta = prev.delta
                if iv is None and prev is not None:
                    iv = prev.iv
                when = None
                if quote is not None:
                    when = _ms_to_et(getattr(quote, "bid_time", None) or getattr(quote, "event_time", None))
                if when is None and prev is not None:
                    when = prev.time
                self._quotes[key] = OptQuote(
                    bid=bid, ask=ask, delta=delta, iv=iv, time=when, mdtype=1,
                    con_id=meta.get("occ"), volume=None,
                )

    def option_quotes(self):
        if self.transport is not None:
            book = getattr(self.transport, "option_quotes", None)
            if callable(book):
                return dict(book())
            return {}
        with self._quote_lock:
            return dict(self._quotes)

    def preview_combo(self, legs, action, qty, limit, tif="DAY"):
        payload = build_vertical_order(legs, action, int(qty), float(limit), tif)
        self._assert_order_session(self.order_session)
        if self.transport is not None:
            result = self.transport.preview_order(self.order_session, payload)
            result.setdefault("order", payload)
            return result
        response = self._call(self._transmit(payload, dry_run=True))
        errors = [str(item) for item in (getattr(response, "errors", None) or [])]
        warnings = [str(item) for item in (getattr(response, "warnings", None) or [])]
        return {"ok": not errors, "rejects": errors, "warnings": warnings, "order": payload}

    def approve_ticket(self, ticket_id, cap):
        self.guard.approve(ticket_id, float(cap))
        self.ticket = ticket_id
        self.store.setdefault("tickets", {})[ticket_id] = {
            "cap": float(cap), "approved_at": datetime.now(ET).isoformat(),
        }
        self._save()

    def finish_ticket(self):
        if self.ticket:
            self.guard.close(self.ticket)
            self.store.setdefault("tickets", {}).setdefault(self.ticket, {})["finished"] = True
            self._save()

    def _need_ticket(self) -> str:
        if not self.cfg.live:
            raise SafetyError("order call outside live mode")
        ticket_id = self.ticket
        if not ticket_id or ticket_id not in self.guard.approved:
            raise guard.GuardError("refused: no approved ticket for this trade")
        return ticket_id

    def _wait_rate(self, kind: str, order_id=None):
        wait = self.guard.wait_secs(kind, None if kind == "place" else order_id)
        if wait > 0:
            if wait > 30:
                raise guard.GuardError(f"{kind} refused: rate limit, retry in {wait:.0f}s")
            _time.sleep(wait + 0.05)

    def _gate(self, kind: str, *, action: str | None = None, limit: float | None = None, order_id=None) -> str:
        self._assert_order_session(self.order_session)
        ticket_id = self._need_ticket()
        if action == "BUY" and limit is not None and float(limit) > self.guard.cap(ticket_id) + 1e-9:
            raise guard.GuardError(
                f"refused: {float(limit):.2f} is above the approved cap {self.guard.cap(ticket_id):.2f}")
        self._wait_rate(kind, order_id)
        self.guard.authorize(kind, ticket_id, None if kind == "place" else order_id)
        return ticket_id

    def _require_ref(self, order_ref) -> str:
        if not str(order_ref or "").startswith(ORDER_PREFIX):
            raise SafetyError(f"every Noon order must carry an {ORDER_PREFIX} ref")
        return str(order_ref)

    def place_combo(self, legs, action, qty, limit, account, order_ref=None, tif="DAY"):
        ref = self._require_ref(order_ref)
        payload = build_vertical_order(legs, action, int(qty), float(limit), tif, external_id=ref)
        self._gate("place", action=action, limit=float(limit))
        if self.transport is not None:
            order_id = str(self.transport.place_order(self.order_session, payload))
        else:
            response = self._call(self._transmit(payload, dry_run=False, account_number=str(account)))
            errors = getattr(response, "errors", None) or []
            if errors:
                raise RuntimeError("; ".join(str(item) for item in errors))
            order_id = str(response.order.id)
        self.guard.bind(order_id, self.ticket)
        self.store.setdefault("orders", {})[order_id] = {
            "ticket": self.ticket,
            "ref": ref,
            "action": action,
            "qty": int(qty),
            "legs": [[str(symbol), int(ratio)] for symbol, ratio in legs],
            "limit": float(floor_tick(float(limit))),
            "tif": tif,
            "current": order_id,
            "account": str(account),
            "host": session_host(self.order_session),
            "real_money": self.cfg.tasty_orders == "production",
            "placed_at": datetime.now(ET).isoformat(),
        }
        self._save()
        return order_id

    def replace_combo(self, handle, limit):
        """Safety-locked replace. The noon session does not call this; it does not chase."""
        meta = self._own(handle)
        if meta is None:
            raise SafetyError(f"refusing to replace order {handle}: not a Noon order")
        payload = build_vertical_order(
            meta["legs"], meta["action"], int(meta["qty"]), float(limit), meta.get("tif") or "DAY",
            external_id=meta.get("ref"))
        self._gate("replace", action=meta["action"], limit=float(limit), order_id=str(meta["current"]))
        if self.transport is not None:
            new_id = str(self.transport.replace_order(self.order_session, meta["current"], payload))
        else:
            replaced = self._call(self._replace_async(meta, payload))
            new_id = str(getattr(replaced, "id", "") or "")
            if not new_id:
                raise RuntimeError("tastytrade replace returned no order id")
        self.guard.bind(new_id, meta["ticket"])
        meta = dict(meta)
        meta["current"] = new_id
        meta["limit"] = float(floor_tick(float(limit)))
        meta["replaced_at"] = datetime.now(ET).isoformat()
        self.store.setdefault("orders", {})[str(handle)] = meta
        self.store["orders"][new_id] = meta
        self._save()
        return new_id

    def modify_combo(self, handle, limit):
        raise SafetyError("refusing to reprice: noon breakout does not chase")

    async def _transmit(self, payload: dict, *, dry_run: bool, account_number: str | None = None):
        self._assert_order_session(self.order_session)
        account = await self._account_for(account_number)
        order = _limit_order(payload)
        return await account.place_order(self.order_session, order, dry_run=dry_run)

    async def _replace_async(self, meta, payload):
        self._assert_order_session(self.order_session)
        account = await self._account_for(meta.get("account"))
        order = _limit_order(payload)
        return await account.replace_order(self.order_session, int(meta["current"]), order)

    async def _account_for(self, account_number: str | None):
        if not self._account_objs:
            rows = await self._fetch_accounts()
            self._account_objs = {str(row.account_number): row for row in rows}
        if account_number and account_number in self._account_objs:
            return self._account_objs[account_number]
        if len(self._account_objs) == 1:
            return next(iter(self._account_objs.values()))
        raise SafetyError(f"tastytrade account {account_number} is not on the order session")

    def _own(self, handle):
        found = self.store.get("orders", {}).get(str(handle))
        if found is None:
            log.warning("refusing: order %s was not placed by Noon Breakout", handle)
        return found

    def cancel(self, handle):
        meta = self._own(handle)
        if meta is None:
            return
        state = self.order_state(handle)
        if state["done"]:
            return
        self._gate("cancel", order_id=str(meta["current"]))
        if self.transport is not None:
            self.transport.cancel_order(self.order_session, meta["current"])
            return
        try:
            self._call(self._cancel_async(meta))
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel %s failed: %s", meta["current"], exc.__class__.__name__)

    async def _cancel_async(self, meta):
        self._assert_order_session(self.order_session)
        account = await self._account_for(meta.get("account"))
        await account.delete_order(self.order_session, int(meta["current"]))

    def order_state(self, handle) -> dict:
        meta = self.store.get("orders", {}).get(str(handle))
        if meta is None:
            raise KeyError(handle)
        if self.transport is not None:
            order = self.transport.get_order(self.order_session, meta["current"])
        else:
            order = self._call(self._get_order(meta))
        raw = _status_name(getattr(order, "status", ""))
        qty = float(meta["qty"])
        if raw in FILLED:
            filled = qty
            avg = _fill_price(order, meta.get("action") or "BUY", meta["limit"])
            status = "Filled"
        elif raw in CANCELLED:
            filled = 0.0
            avg = 0.0
            status = "Cancelled"
        else:
            filled = 0.0
            avg = 0.0
            status = "Submitted"
        return {
            "status": status,
            "filled": filled,
            "remaining": max(qty - filled, 0.0),
            "avg_price": avg or 0.0,
            "done": status in ("Filled", "Cancelled") or raw in TERMINAL,
        }

    async def _get_order(self, meta):
        self._assert_order_session(self.order_session)
        account = await self._account_for(meta.get("account"))
        return await account.get_order(self.order_session, int(meta["current"]))

    def own_open_orders(self):
        out = []
        for order_id, meta in self.store.get("orders", {}).items():
            if not str(meta.get("ref") or "").startswith(ORDER_PREFIX):
                continue
            if str(meta.get("current") or order_id) != str(order_id):
                continue
            try:
                state = self.order_state(order_id)
            except Exception:  # noqa: BLE001
                continue
            if state["done"]:
                continue
            out.append({
                "order_id": order_id,
                "action": meta["action"],
                "limit": meta["limit"],
                "ref": meta["ref"],
                "status": state["status"],
            })
        return out

    def _save(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.store, indent=1, default=str))
        tmp.replace(self.store_path)


def _pad_occ(occ: str) -> str:
    """OCC root is six characters. ``SPXW  251104C07000000`` already is."""
    text = str(occ or "")
    if not text or text.startswith("."):
        return text
    if " " in text[:6]:
        return text
    split = 0
    while split < len(text) and text[split].isalpha():
        split += 1
    if split == 0 or split >= 6:
        return text
    return text[:split].ljust(6) + text[split:]


def _limit_order(payload: dict):
    from decimal import Decimal

    from tastytrade.order import InstrumentType, Leg, LimitOrder, OrderAction, OrderTimeInForce

    legs = [
        Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=leg["symbol"],
            action=OrderAction(leg["action"]),
            quantity=1,
        )
        for leg in payload["legs"]
    ]
    tif = OrderTimeInForce.GTC if payload["time-in-force"] == "GTC" else OrderTimeInForce.DAY
    return LimitOrder(
        time_in_force=tif,
        legs=legs,
        price=Decimal(str(payload["price"])),
        external_identifier=payload.get("external-identifier"),
    )
