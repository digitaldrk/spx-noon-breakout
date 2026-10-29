"""Trade log. One row per attempt, with every field from plan §8.11 on a completed trade."""
from __future__ import annotations

import csv
import json
import math
import os
from datetime import date, datetime
from pathlib import Path

TRADE_COLS = [
    "date", "timestamp", "direction", "spx", "pdh", "pdl", "expected_move", "width",
    "long_strike", "short_strike", "long_delta", "short_delta",
    "spread_mid", "fill_price", "slippage_vs_mid", "slippage_per_side",
    "exit_time", "exit_reason", "exit_fill", "exit_slippage_vs_mid", "exit_slippage_per_side",
    "pnl", "fees", "mode", "note",
]


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return f"{value:.4f}".rstrip("0").rstrip(".") if abs(value) < 1 else f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class Outputs:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def trade_path(self) -> Path:
        return self.dir / "trades.csv"

    def append_trade(self, row: dict) -> None:
        path = self.trade_path()
        new = not path.exists()
        with path.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_COLS, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerow({k: _cell(row.get(k)) for k in TRADE_COLS})

    def read_trades(self, day: date | None = None) -> list[dict]:
        path = self.trade_path()
        if not path.exists():
            return []
        with path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        if day is None:
            return rows
        key = day.isoformat()
        return [r for r in rows if r.get("date") == key]

    def write_status(self, day: date, status: dict) -> None:
        _atomic_write(self.dir / f"status_{day.isoformat()}.json", json.dumps(status, indent=2, default=str))

    def write_report(self, day: date, text: str) -> Path:
        path = self.dir / f"report_{day.isoformat()}.md"
        _atomic_write(path, text)
        return path
