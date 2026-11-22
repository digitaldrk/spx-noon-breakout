"""Console alerts and logs/alerts.log."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class Notifier:
    def __init__(self, logs_dir: Path):
        self.path = Path(logs_dir) / "alerts.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def alert(self, title: str, body: str) -> None:
        stamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
        block = f"\n===== {stamp} | {title} =====\n{body}\n"
        print(block, flush=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(block)
