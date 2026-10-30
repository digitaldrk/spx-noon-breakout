"""Configuration. The computer picks the broker. A config file cannot.

Mac (darwin) is IBKR TWS paper, 127.0.0.1:7497, account ids starting with D.
Windows is Tod's Schwab account. mode "live" sends only after `yes <ticketID>`.
mode "dry" (the default) and "alerts" never transmit.

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

PAPER_PORT = 7497
ORDER_PREFIX = "SPXNOON-"


class SafetyError(RuntimeError):
    """A hard safety rule failed. The bot must not send orders."""


def _t(value: str) -> time:
    parts = [int(x) for x in str(value).split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2])


def machine_broker() -> str:
    if sys.platform == "darwin":
        return "ibkr"
    if sys.platform == "win32":
        return "schwab"
    raise SafetyError(
        f"Noon Breakout runs on Mac (IBKR paper) or Windows (Schwab); this is {sys.platform}")


@dataclass
class Config:
    broker: str = ""
    dry_run: bool = True
    mode: str = ""
    account: str = ""

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
        if not self.broker:
            if sys.platform == "darwin":
                self.broker = "ibkr"
            elif sys.platform == "win32":
                self.broker = "schwab"
        if self.broker in ("ibkr", "sim"):
            if not self.mode:
                self.mode = "dry" if self.dry_run else "paper"
            if self.mode in ("dry", "alerts"):
                self.dry_run = True
            elif self.mode == "paper":
                self.dry_run = False
        elif self.broker == "schwab":
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
        """Schwab live. Orders go out only after Tod types yes."""
        return self.broker == "schwab" and self.mode == "live" and not self.dry_run

    @property
    def paper(self) -> bool:
        """IBKR paper on port 7497. The port and a D-account are the gate."""
        return self.broker == "ibkr" and self.mode == "paper" and not self.dry_run

    def sends_orders(self) -> bool:
        """True when this process may hand an order to the broker adapter."""
        if self.mode in ("dry", "alerts"):
            return False
        if self.broker == "sim":
            return self.mode == "paper"
        return self.paper or self.live

    def validate(self) -> None:
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
        elif self.broker == "sim":
            if self.mode not in ("dry", "alerts", "paper"):
                raise SafetyError(f"sim mode must be dry, alerts, or paper (got {self.mode!r})")
        else:
            raise SafetyError("Noon Breakout runs on Mac (IBKR paper) or Windows (Schwab)")
        if self.decision_grace_s <= 0:
            raise ValueError("decision_grace_s must be positive")
        if self.chain_band_pts <= 0:
            raise ValueError("chain_band_pts must be positive")

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
    if "broker" not in overrides and sys.platform not in ("darwin", "win32"):
        overrides["broker"] = "ibkr"
    if os.environ.get("SCHWAB_TOKEN_PATH"):
        kwargs["token_path"] = os.environ["SCHWAB_TOKEN_PATH"]
    if os.environ.get("SPX_NOON_HOME") and "base_dir" not in data:
        kwargs["base_dir"] = os.environ["SPX_NOON_HOME"]
    kwargs.update(overrides)
    cfg = Config(**kwargs)
    cfg.validate()
    return cfg
