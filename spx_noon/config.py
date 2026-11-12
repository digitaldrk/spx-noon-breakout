"""Configuration. The computer picks the broker. A config file cannot.

Mac (darwin) is IBKR TWS. mode "paper" is 127.0.0.1:7497 with account ids
starting with D. mode "live" is real money on 127.0.0.1:7496 (TWS) or 4001
(IB Gateway) and needs the ``SPX_NOON_ALLOW_LIVE`` / ``SPX_NOON_LIVE_ACCOUNT``
latch in the environment. A config file alone can never enable real orders.
Windows is tastytrade. ``tasty_data`` (default production) and ``tasty_orders``
(default sandbox) choose the hosts. There is no env-var toggle.
``tasty_orders=production`` is real money and requires ``tasty_data=production``.
Tastytrade orders auto-approve. Schwab, selected with ``--broker schwab``, still
waits for ``yes <ticketID>``. mode "dry" (the default) and "alerts" never transmit.

Plan v2 version B prices (75 delta, +30%, $0.20/side, 2h flat, 1 spread) live in
strategy.py. They are not config keys.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, fields
from datetime import time
from pathlib import Path

PAPER_PORT = 7497
LIVE_PORTS = (7496, 4001)
LOCALHOSTS = ("127.0.0.1", "localhost", "::1")
ORDER_PREFIX = "SPXNOON-"

#: Both must be present and exact before IBKR mode ``live`` may send a real order.
LIVE_ALLOW_ENV = "SPX_NOON_ALLOW_LIVE"
LIVE_ACCOUNT_ENV = "SPX_NOON_LIVE_ACCOUNT"

_order_log = logging.getLogger("spx_noon.orders")


class SafetyError(RuntimeError):
    """A hard safety rule failed. The bot must not send orders."""


def account_tail(account) -> str:
    """Last four characters only. The full account id is never printed or logged."""
    return str(account or "")[-4:]


class _AccountFilter(logging.Filter):
    """Shrink a full account id to its last four characters wherever it appears."""

    def __init__(self, account: str):
        super().__init__()
        self.account = str(account)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.account:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if self.account not in message:
            return True
        record.msg = message.replace(self.account, "..." + account_tail(self.account))
        record.args = ()
        return True


def redact_account(account) -> None:
    """Keep an account id out of every log line, including ``ib_async``'s own order
    logs, which print the whole order at INFO. Handler-level so propagated records
    from third-party loggers are covered too."""
    account = str(account or "")
    if not account:
        return
    for handler in logging.getLogger().handlers:
        if not any(isinstance(flt, _AccountFilter) and flt.account == account
                   for flt in handler.filters):
            handler.addFilter(_AccountFilter(account))


def live_latch(account) -> tuple[bool, str]:
    """``SPX_NOON_ALLOW_LIVE=1`` plus an exact ``SPX_NOON_LIVE_ACCOUNT`` match.

    No strip and no casefold: ``"1 "``, ``"true"``, and a different case all fail.
    """
    want = str(account)
    if not want.strip():
        return False, "IBKR live needs account set to the live account id"
    if want.startswith("D"):
        return False, "IBKR live refuses a paper account id (starts with D)"
    if os.environ.get(LIVE_ALLOW_ENV) != "1":
        return False, (
            f"IBKR live needs {LIVE_ALLOW_ENV}=1 in the environment; "
            "a config file cannot turn on real money")
    if os.environ.get(LIVE_ACCOUNT_ENV) != want:
        return False, f"{LIVE_ACCOUNT_ENV} must equal the config account exactly"
    return True, ""


def _env_name(value, key: str) -> str:
    text = str(value or "").strip().lower()
    if text not in ("sandbox", "production"):
        raise SafetyError(f"{key} must be 'sandbox' or 'production' (got {value!r})")
    return text


def _t(value: str) -> time:
    parts = [int(x) for x in str(value).split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2])


_BROKER_ALIASES = {
    "tt": "tasty",
    "tastytrade": "tasty",
    "tasty": "tasty",
    "ibkr": "ibkr",
    "schwab": "schwab",
    "sim": "sim",
}


def machine_broker() -> str:
    if sys.platform == "darwin":
        return "ibkr"
    if sys.platform == "win32":
        return "tasty"
    raise SafetyError(
        f"Noon Breakout runs on Mac (IBKR) or Windows (tastytrade); this is {sys.platform}")


def resolve_broker(name: str | None = None) -> str:
    """``auto`` follows the machine. ``tasty`` / ``tt`` / ``tastytrade`` are one broker."""
    key = str(name or "auto").strip().lower()
    if key in ("", "auto"):
        if sys.platform == "win32":
            return "tasty"
        return "ibkr"
    found = _BROKER_ALIASES.get(key)
    if found is None:
        raise SafetyError(
            f"unknown broker {name!r}; use auto, tasty, tt, tastytrade, ibkr, or schwab")
    return found


@dataclass
class Config:
    broker: str = ""
    dry_run: bool = True
    mode: str = ""
    account: str = ""
    tasty_data: str = "production"
    tasty_orders: str = "sandbox"

    host: str = "127.0.0.1"
    port: int = PAPER_PORT
    client_id: int = 41
    check_client_id: int = 141
    wrap_client_id: int = 142
    tws_naive_tz: str = "US/Central"

    token_path: str = "~/schwab/tokens.json"
    approval_timeout_secs: float = 300.0
    max_orders_per_min: int = 10
    min_replace_secs: float = 15.0
    poll_secs: float = 3.0
    toast: bool = True
    connect_timeout_s: float = 15.0
    connect_retries: int = 6
    base_dir: str = ""

    start_not_before: str = "11:45:00"
    decision_grace_s: float = 90.0
    loop_interval_s: float = 5.0
    exit_wait_s: float = 20.0
    chain_band_pts: float = 150.0
    max_option_lines: int = 120

    def t(self, name: str) -> time:
        value = getattr(self, name)
        return value if isinstance(value, time) else _t(value)

    @property
    def home(self) -> Path:
        return Path(os.path.expanduser(self.base_dir))

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.reports_dir, self.home / "logs"):
            path.mkdir(parents=True, exist_ok=True)

    def __post_init__(self) -> None:
        self.broker = _BROKER_ALIASES.get(str(self.broker or "").strip().lower(), str(self.broker or "").strip().lower())
        if self.broker == "auto":
            self.broker = ""
        if not self.broker:
            if sys.platform == "darwin":
                self.broker = "ibkr"
            elif sys.platform == "win32":
                self.broker = "tasty"
        if self.broker in ("ibkr", "sim"):
            if not self.mode:
                self.mode = "dry" if self.dry_run else "paper"
            if self.mode in ("dry", "alerts"):
                self.dry_run = True
            elif self.mode == "paper":
                self.dry_run = False
            elif self.mode == "live" and self.broker == "ibkr":
                self.dry_run = False
        elif self.broker in ("schwab", "tasty"):
            if not self.mode:
                self.mode = "dry" if self.dry_run else "live"
            if self.mode in ("dry", "alerts"):
                self.dry_run = True
            elif self.mode == "live":
                self.dry_run = False
        if not self.base_dir:
            self.base_dir = (
                "~/Documents/spx-noon-breakout" if self.broker in ("ibkr", "sim") else "~/spx-noon-breakout"
            )

    @property
    def live(self) -> bool:
        """Schwab or tastytrade live. Schwab still waits for ``yes``. Tastytrade does not."""
        return self.broker in ("schwab", "tasty") and self.mode == "live" and not self.dry_run

    @property
    def confirms_orders(self) -> bool:
        """Human ``yes <id>`` gate. Tastytrade auto-approves and does not use this."""
        return self.broker == "schwab" and self.live

    @property
    def real_money(self) -> bool:
        """Tastytrade order host is production. Sandbox orders are not real money."""
        return self.broker == "tasty" and str(self.tasty_orders).strip().lower() == "production"

    @property
    def paper(self) -> bool:
        """IBKR paper on port 7497. The port and a D-account are the gate."""
        return self.broker == "ibkr" and self.mode == "paper" and not self.dry_run

    @property
    def ibkr_live(self) -> bool:
        """IBKR real money. Deliberately not ``live``: it must not take the
        Schwab/tastytrade preview-and-``yes`` path in the runner."""
        if self.broker != "ibkr" or self.mode != "live" or self.dry_run:
            return False
        return live_latch(self.account)[0]

    @property
    def real_money_flag(self) -> str:
        """``YES`` only for a latched IBKR live session. Paper, dry, alerts are ``NO``."""
        return "YES" if self.ibkr_live else "NO"

    def sends_orders(self) -> bool:
        """True when this process may hand an order to the broker adapter."""
        if self.mode in ("dry", "alerts"):
            return False
        if self.broker == "sim":
            return self.mode == "paper"
        return self.paper or self.live or self.ibkr_live

    def validate(self) -> None:
        self.tasty_data = _env_name(self.tasty_data, "tasty_data")
        self.tasty_orders = _env_name(self.tasty_orders, "tasty_orders")
        if self.tasty_orders == "production" and self.tasty_data != "production":
            raise SafetyError("tasty_orders=production requires tasty_data=production")
        if self.broker == "ibkr":
            if self.mode not in ("dry", "alerts", "paper", "live"):
                raise SafetyError(
                    f"Mac/IBKR mode must be dry, alerts, paper, or live (got {self.mode!r})")
            if self.mode not in ("paper", "live") and not self.dry_run:
                raise SafetyError("dry_run stays on unless mode is 'paper' or 'live'")
            if self.mode == "live":
                allowed, why = live_latch(self.account)
                if not allowed:
                    raise SafetyError(f"Refusing to run live: {why}")
                if int(self.port) not in LIVE_PORTS:
                    raise SafetyError(
                        f"Refusing to run: port {self.port} is not an IBKR live port "
                        f"{LIVE_PORTS[0]} (TWS) or {LIVE_PORTS[1]} (IB Gateway).")
            elif int(self.port) != PAPER_PORT:
                raise SafetyError(
                    f"Refusing to run: port {self.port} is not the IBKR paper port {PAPER_PORT}.")
            if self.host not in LOCALHOSTS:
                raise SafetyError(f"Refusing to run: host {self.host!r} is not localhost.")
            if int(self.wrap_client_id) == int(self.client_id) or int(self.check_client_id) == int(self.client_id):
                raise SafetyError("check_client_id and wrap_client_id must differ from client_id")
        elif self.broker == "schwab":
            if self.mode not in ("dry", "alerts", "live"):
                raise SafetyError(f"Windows/Schwab mode must be dry, alerts, or live (got {self.mode!r})")
            if self.mode != "live" and not self.dry_run:
                raise SafetyError("dry_run stays on unless mode is 'live'")
            if self.mode == "live" and not str(self.account).strip():
                raise SafetyError("Schwab live needs account set to the account's last digits")
            if self.max_orders_per_min > 10:
                raise SafetyError("max_orders_per_min cannot exceed Schwab's 10/min")
        elif self.broker == "tasty":
            if self.mode not in ("dry", "alerts", "live"):
                raise SafetyError(f"tastytrade mode must be dry, alerts, or live (got {self.mode!r})")
            if self.mode != "live" and not self.dry_run:
                raise SafetyError("dry_run stays on unless mode is 'live'")
            if self.max_orders_per_min > 10:
                raise SafetyError("max_orders_per_min cannot exceed 10")
        elif self.broker == "sim":
            if self.mode not in ("dry", "alerts", "paper"):
                raise SafetyError(f"sim mode must be dry, alerts, or paper (got {self.mode!r})")
        else:
            raise SafetyError("Noon Breakout runs on Mac (IBKR) or Windows (tastytrade)")
        if self.decision_grace_s <= 0:
            raise ValueError("decision_grace_s must be positive")
        if self.chain_band_pts <= 0:
            raise ValueError("chain_band_pts must be positive")

    def to_dict(self) -> dict:
        return asdict(self)


def startup_banner(cfg, account) -> str:
    """One line, printed and logged once the IBKR account has been verified.

    Only the last four characters of the verified account appear.
    """
    return (
        f"BROKER={cfg.broker} MODE={cfg.mode} ACCOUNT={account_tail(account)} "
        f"PORT={int(cfg.port)} REAL MONEY={cfg.real_money_flag}"
    )


def order_log_suffix(cfg) -> str:
    """``REAL MONEY=YES|NO`` for IBKR order lines. Other brokers log unchanged."""
    if getattr(cfg, "broker", "") != "ibkr":
        return ""
    return f" REAL MONEY={cfg.real_money_flag}"


def log_order(cfg, level, msg, *args, **kwargs) -> None:
    """Log an order line. IBKR lines carry the real-money flag; nothing logs an account id."""
    _order_log.log(level, str(msg) + order_log_suffix(cfg), *args, **kwargs)


def load_config(path: str | os.PathLike | None = None, **overrides) -> Config:
    """Load config.json over the defaults, then apply keyword overrides.

    A ``broker`` key inside the file is ignored. Tests may pass ``broker=``.
    A ``dry_run`` key inside the file is ignored. ``mode`` sets that flag.
    """
    data: dict = {}
    if path is None:
        path = os.environ.get("SPX_NOON_CONFIG")
        if path is None:
            cand = Path(__file__).resolve().parent.parent / "config.json"
            path = cand if cand.exists() else None
    if path is not None and Path(path).exists():
        with open(path) as fh:
            data = json.load(fh)
    known = {f.name for f in fields(Config)}
    kwargs = {
        k: (tuple(v) if isinstance(v, list) else v)
        for k, v in data.items()
        if k in known and k not in ("broker", "dry_run") and not str(k).startswith("_")
    }
    if "broker" in overrides:
        overrides["broker"] = resolve_broker(overrides["broker"])
    elif sys.platform not in ("darwin", "win32"):
        overrides["broker"] = "ibkr"
    if os.environ.get("SCHWAB_TOKEN_PATH"):
        kwargs["token_path"] = os.environ["SCHWAB_TOKEN_PATH"]
    if os.environ.get("SPX_NOON_HOME") and "base_dir" not in data:
        kwargs["base_dir"] = os.environ["SPX_NOON_HOME"]
    kwargs.update(overrides)
    cfg = Config(**kwargs)
    cfg.validate()
    return cfg
