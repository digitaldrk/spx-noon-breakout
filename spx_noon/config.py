"""Configuration. ``config.json`` names the broker, and nothing else does.

``broker`` is ``ibkr`` or ``tasty``, and a missing or blank one refuses to load.
The Mac template says ``ibkr``, the Windows and Render templates say ``tasty``.
On the hosted worker a non-blank ``BROKER`` overrides the file, through the
keyword overrides :mod:`spx_noon.service` hands this module.
Mac runs IBKR TWS. There is one job and one config: ``mode``, ``port``,
and ``account`` are the switch, together with the TWS session the operator is
logged into. mode "paper" is 127.0.0.1:7497 with account ids starting with D.
mode "live" is real money on 127.0.0.1:7496 (TWS) or 4001 (IB Gateway) with an
``account`` that is set and does not start with D.
Windows is tastytrade. ``tasty_data`` (default production) and ``tasty_orders``
(default sandbox) choose the hosts.
``tasty_orders=production`` is real money and requires ``tasty_data=production``.
No field here is an environment knob. ``SPX_NOON_HOME`` fills ``base_dir`` when
the file has none and ``SPX_NOON_CONFIG`` names the default file; both belong to
the local Mac and Windows launchers. The hosted worker reads its own plain
variables in :mod:`spx_noon.service`, resolves them there, and hands its children
the resolved file.
Tastytrade orders auto-approve: no console prompt takes part. mode "dry" (the
default) and "alerts" never transmit.

``plan`` is the one trading rule that is a config key: ``B`` (the default, and
what a missing key means) or ``A1``. It is read from the file, never from the
environment, and each operator picks it in their own ``config.json``. Everything
else about both plans (75 or 80 delta, +30%, $0.20/side, 2h flat, A1's morning
skip and +8 points, 1 spread) lives in strategy.py and is not a config key.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from datetime import time
from pathlib import Path

from .strategy import PLANS

PAPER_PORT = 7497
LIVE_PORTS = (7496, 4001)
LOCALHOSTS = ("127.0.0.1", "localhost", "::1")
ORDER_PREFIX = "SPXNOON-"

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


def live_account_ok(account) -> tuple[bool, str]:
    """The account half of the live gate: set, and not an IBKR paper id.

    No environment variable is read. ``config.json`` plus the TWS session the
    operator is logged into is the switch. The port and host halves are in
    :meth:`Config.validate`, and TWS is asked to confirm the account after connect.
    """
    want = str(account)
    if not want.strip():
        return False, "IBKR live needs account set to the live account id"
    if want.startswith("D"):
        return False, "IBKR live refuses a paper account id (starts with D)"
    return True, ""


def _env_name(value, key: str) -> str:
    text = str(value or "").strip().lower()
    if text not in ("sandbox", "production"):
        raise SafetyError(f"{key} must be 'sandbox' or 'production' (got {value!r})")
    return text


def plan_name(value) -> str:
    """``B`` or ``A1``, after stripping whitespace and upper-casing.

    ``b`` and ``a1`` canonicalize. Anything else, including a blank string, is a
    :class:`SafetyError`: a plan the code does not have must not quietly become
    version B. The valid names are :data:`spx_noon.strategy.PLANS`.
    """
    key = str(value or "").strip().upper()
    if key not in PLANS:
        raise SafetyError(f"plan must be {' or '.join(PLANS)} (got {value!r})")
    return key


def _t(value: str) -> time:
    parts = [int(x) for x in str(value).split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2])


#: One name each. These are the only broker values ``config.json`` may hold.
#: ``sim`` is the in-process test broker and is not a public name: it is valid on
#: :class:`Config` only, never out of :func:`resolve_broker`.
VALID_BROKERS = ("ibkr", "tasty")

#: What a missing or blank broker says, wherever it is caught.
MISSING_BROKER = "broker must be set in config.json: ibkr or tasty"


def resolve_broker(name: str | None = None) -> str:
    """``ibkr`` and ``tasty`` are the only broker names. A missing name is refused.

    No environment variable and no operating system take part: the config file
    names the broker, and on the hosted worker ``BROKER`` overrides that file.
    ``auto`` and ``sim`` are unknown brokers here.
    """
    key = str(name or "").strip().lower()
    if not key:
        raise SafetyError(MISSING_BROKER)
    if key not in VALID_BROKERS:
        raise SafetyError(f"unknown broker {name!r}; use ibkr or tasty")
    return key


@dataclass
class Config:
    broker: str = ""
    dry_run: bool = True
    mode: str = ""
    account: str = ""
    tasty_data: str = "production"
    tasty_orders: str = "sandbox"

    #: The trading plan: ``B`` (default) or ``A1``. ``validate`` canonicalizes
    #: it and refuses anything else. Not an environment knob on any platform.
    plan: str = "B"

    host: str = "127.0.0.1"
    port: int = PAPER_PORT
    client_id: int = 41
    check_client_id: int = 141
    wrap_client_id: int = 142
    tws_naive_tz: str = "US/Central"

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
        # Case and whitespace only. No spelling is remapped to another broker:
        # Config(broker="tt") refuses in validate(), it does not become tasty.
        self.broker = str(self.broker or "").strip().lower()
        if self.broker in ("ibkr", "sim"):
            if not self.mode:
                self.mode = "dry" if self.dry_run else "paper"
            if self.mode in ("dry", "alerts"):
                self.dry_run = True
            elif self.mode == "paper":
                self.dry_run = False
            elif self.mode == "live" and self.broker == "ibkr":
                self.dry_run = False
        elif self.broker == "tasty":
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
        """Tastytrade live: preview, then auto-approve. No console prompt."""
        return self.broker == "tasty" and self.mode == "live" and not self.dry_run

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
        tastytrade preview-and-approve path in the runner.

        ``validate()`` has already accepted the port, the host, and the account
        for any config that reaches a broker, so this is the mode alone."""
        return self.broker == "ibkr" and self.mode == "live" and not self.dry_run

    @property
    def real_money_flag(self) -> str:
        """``YES`` only for an IBKR live session. Paper, dry, alerts are ``NO``."""
        return "YES" if self.ibkr_live else "NO"

    def sends_orders(self) -> bool:
        """True when this process may hand an order to the broker adapter."""
        if self.mode in ("dry", "alerts"):
            return False
        if self.broker == "sim":
            return self.mode == "paper"
        return self.paper or self.live or self.ibkr_live

    def validate(self) -> None:
        self.plan = plan_name(self.plan)
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
                allowed, why = live_account_ok(self.account)
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
        elif not self.broker:
            raise SafetyError(MISSING_BROKER)
        else:
            raise SafetyError(f"unknown broker {self.broker!r}; use ibkr or tasty")
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

    The file's ``broker`` key is the broker, and a keyword ``broker=`` wins over
    it: that is how the worker's ``BROKER`` arrives. Neither one set is a
    :class:`SafetyError`.
    A ``dry_run`` key inside the file is ignored. ``mode`` sets that flag.
    No config field is read from the environment, ``BROKER`` included. ``plan``,
    ``mode``, ``tasty_data``, ``tasty_orders``, and ``account`` come from the
    file or from a keyword — there is no ``PLAN`` variable on any platform — and
    the hosted worker resolves its own plain variables in
    :mod:`spx_noon.service` before its children ever call this.
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
        if k in known and k != "dry_run" and not str(k).startswith("_")
    }
    if os.environ.get("SPX_NOON_HOME") and "base_dir" not in data:
        kwargs["base_dir"] = os.environ["SPX_NOON_HOME"]
    kwargs.update(overrides)
    kwargs["broker"] = resolve_broker(kwargs.get("broker"))
    cfg = Config(**kwargs)
    cfg.validate()
    return cfg
