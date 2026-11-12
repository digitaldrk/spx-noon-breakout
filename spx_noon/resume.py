"""Our own position file. The bot never adopts another strategy's SPX position.

Paper, dry, and alerts use ``own_position.json``. IBKR live uses
``own_position-live.json`` so a paper position can never be flattened on the
live account. The rules for both files are the same.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path


def path_for(state_dir: Path, live: bool = False) -> Path:
    return Path(state_dir) / ("own_position-live.json" if live else "own_position.json")


def load(state_dir: Path, live: bool = False) -> dict | None:
    path = path_for(state_dir, live)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def already_filled(state_dir: Path, day: date, live: bool = False) -> bool:
    """True after a fill has been closed. A skip does not set this."""
    data = load(state_dir, live)
    if not data or data.get("date") != day.isoformat():
        return False
    return bool(data.get("had_fill")) and data.get("status") == "closed"


def load_open(state_dir: Path, day: date, live: bool = False) -> dict | None:
    data = load(state_dir, live)
    if not data or data.get("status") not in ("open", "working"):
        return None
    if data.get("date") != day.isoformat():
        return None
    return data


def save(state_dir: Path, data: dict, live: bool = False) -> None:
    path = path_for(state_dir, live)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, path)


def mark_closed(state_dir: Path, live: bool = False) -> None:
    data = load(state_dir, live)
    if not data:
        return
    data["status"] = "closed"
    save(state_dir, data, live)
