"""Configuration. The computer picks the broker. A config file cannot.

Mac (darwin) is IBKR TWS paper, 127.0.0.1:7497, account ids starting with D.
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
import os
import sys
from dataclasses import asdict, dataclass, fields
from datetime import time
from pathlib import Path

from .pricing import (
    PAPER_ENTRY_REPRICE_ATTEMPTS,
    PAPER_ENTRY_REPRICE_MAX_ADD,
    PAPER_ENTRY_REPRICE_SECS,
    REPRICE_MAX_ADD_CEILING,
    TICK,
)

PAPER_PORT = 7497
ORDER_PREFIX = "SPXNOON-"


class SafetyError(RuntimeError):
    """A hard safety rule failed. The bot must not send orders."""


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
        f"Noon Breakout runs on Mac (IBKR paper) or Windows (tastytrade); this is {sys.platform}")


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
    # Paper evaluation only. Live tastytrade and Schwab ignore this flag.
    entry_reprice: bool = True
    entry_reprice_max_add: float = PAPER_ENTRY_REPRICE_MAX_ADD
    entry_reprice_secs: float = PAPER_ENTRY_REPRICE_SECS
    entry_reprice_attempts: int = PAPER_ENTRY_REPRICE_ATTEMPTS
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

    def entry_reprice_on(self) -> bool:
        """True only for a paper session. Live, dry, and alerts never reprice."""
        if not self.entry_reprice or self.dry_run or self.live:
            return False
        if self.broker == "ibkr" and self.paper:
            return True
        return self.broker == "sim" and self.mode == "paper"

    def sends_orders(self) -> bool:
        """True when this process may hand an order to the broker adapter."""
        if self.mode in ("dry", "alerts"):
            return False
        if self.broker == "sim":
            return self.mode == "paper"
        return self.paper or self.live

    def validate(self) -> None:
        self.tasty_data = _env_name(self.tasty_data, "tasty_data")
        self.tasty_orders = _env_name(self.tasty_orders, "tasty_orders")
        if self.tasty_orders == "production" and self.tasty_data != "production":
            raise SafetyError("tasty_orders=production requires tasty_data=production")
        if self.broker == "ibkr":
            if self.mode not in ("dry", "alerts", "paper"):
                raise SafetyError(f"Mac/IBKR mode must be dry, alerts, or paper (got {self.mode!r})")
            if self.mode != "paper" and not self.dry_run:
                raise SafetyError("dry_run stays on unless mode is 'paper'")
            if int(self.port) != PAPER_PORT:
                raise SafetyError(
                    f"Refusing to run: port {self.port} is not the IBKR paper port {PAPER_PORT}.")
            if self.host not in ("127.0.0.1", "localhost", "::1"):
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
            raise SafetyError("Noon Breakout runs on Mac (IBKR paper) or Windows (tastytrade)")
        if self.decision_grace_s <= 0:
            raise ValueError("decision_grace_s must be positive")
        if self.chain_band_pts <= 0:
            raise ValueError("chain_band_pts must be positive")
        allowance = float(self.entry_reprice_max_add)
        if allowance < 0:
            raise ValueError("entry_reprice_max_add cannot be negative")
        if allowance > REPRICE_MAX_ADD_CEILING + 1e-9:
            raise SafetyError(
                "entry_reprice_max_add is above 0.15. "
                "A walk of 0.40 over the original mid is not enabled.")
        if abs(allowance / TICK - round(allowance / TICK)) > 1e-6:
            raise ValueError("entry_reprice_max_add must be a multiple of 0.05")
        if float(self.entry_reprice_secs) < 15:
            raise ValueError("entry_reprice_secs must be at least 15")
        attempts = int(self.entry_reprice_attempts)
        if attempts < 1 or attempts > PAPER_ENTRY_REPRICE_ATTEMPTS:
            raise ValueError(
                f"entry_reprice_attempts must be from 1 to {PAPER_ENTRY_REPRICE_ATTEMPTS}")
        self.entry_reprice_attempts = attempts

    def to_dict(self) -> dict:
        return asdict(self)


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
