"""One daily row on the shared Google Sheet. Read and write cells only; never a broker.

The 16:20 wrap writes its markdown report first and then calls :func:`push_sheet`
with one row for Boi ``High Noon``. Candle (``spx-breakout``) and Fly
(``spx-fly``) already share that spreadsheet, so the header, the tab rule, and
the ``(Date, Boi)`` upsert here are theirs, copied rather than imported: this
repo does not depend on either of them.

The row goes on the ``Derek Comp`` tab, on the Mac and on the Render Linux
worker. There is no ``sheets_tab`` setting.

Credentials, first match wins: ``SPX_SHEETS_CREDENTIALS_JSON`` (the whole
service-account json, for a host with no disk to keep a key file on — it stays
in memory, is never written to disk, and is never logged or printed), else
config ``sheets_credentials``, else ``SPX_SHEETS_CREDENTIALS``, else
``GOOGLE_APPLICATION_CREDENTIALS``, else ``<home>/state/sheets-service-account.json``.
The sheet must be shared with that service account.

Nothing here raises for a missing key, an unparseable blob, the Google packages
not being installed, or an API error: :func:`push_sheet` returns one status line
and the wrap still exits 0. That line is read by a person and lands at the end
of the markdown report, so it never carries the key, a path's contents, or an
exception message — a failure reports the exception's type and nothing else.
"""
from __future__ import annotations

import json
import os
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

#: The columns Candle and Fly already write, in their order. No other column
#: belongs on that tab, and this report never rewrites anyone else's header.
HEADER = ["Date", "Boi", "Traded", "P&L after fees", "Worst", "What happened"]

#: Column B. One name for this bot, paper book or live book, Mac or Render.
BOI = "High Noon"

#: What ``What happened`` says on a date with no rows at all.
NO_ROWS = "No rows for today."

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

#: The whole service-account json, pasted in the Render dashboard. The wrap
#: child inherits it from the supervisor's environment; it is not a config field
#: and it must never be written into ``service-config.json``.
JSON_ENV = "SPX_SHEETS_CREDENTIALS_JSON"

#: The two file-path variables, in the order they are tried.
PATH_ENVS = ("SPX_SHEETS_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS")

#: The default key file, under the home's ``state/``, which ``.gitignore``
#: already covers twice over. This repo never creates it: an operator does.
KEY_NAME = "sheets-service-account.json"

#: The three columns that say a row is a trade rather than a skip.
FILL_COLS = ("fill_price", "exit_fill", "pnl")


def machine_tab() -> str:
    """The tab this bot writes: ``Derek Comp``.

    The Mac and the Render Linux worker share that tab. There is no setting
    and no operating-system switch.
    """
    return "Derek Comp"


# --------------------------------------------------------------------------
# The row
# --------------------------------------------------------------------------

def _text(value) -> str:
    return str(value or "").strip()


def is_trade(row: dict) -> bool:
    """A filled or closed row. ``skip_no_fill`` and ``skip_morning`` are not trades.

    The book has no traded flag, so the fill columns are the evidence: a row with
    an entry fill, an exit fill, or a P&L happened. Anything else is a day the
    bot looked and did not trade.
    """
    return any(_text(row.get(col)) for col in FILL_COLS)


def pnl_cell(trades: list[dict]) -> str:
    """``P&L after fees`` for the traded rows. Already net of fees; never doubled.

    One row is its own ``pnl`` string, unchanged. Several numeric ones are their
    sum to two places, half a cent rounded away from zero. A blank or unreadable
    ``pnl`` leaves the cell empty rather than reporting a zero that was never
    recorded.

    The sum is :class:`~decimal.Decimal`, not ``float``: the book keeps four
    decimal places (``logbook._cell``), and binary floating point turns a stored
    ``2.675`` into ``2.67`` on the way to two places.
    """
    values = [_text(row.get("pnl")) for row in trades]
    if not values or not all(values):
        return ""
    if len(values) == 1:
        return values[0]
    total = Decimal(0)
    for value in values:
        try:
            total += Decimal(value)
        except InvalidOperation:
            return ""
    if not total.is_finite():
        return ""
    cents = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Decimal keeps the sign through a round to nothing: a tenth of a cent short
    # of flat is "0.00" on the sheet, not "-0.00".
    return str(cents.copy_abs() if cents == 0 else cents)


def row_facts(row: dict) -> str:
    """One row in prose. The same facts the markdown report prints for it."""
    direction = _text(row.get("direction")) or "no direction"
    strikes = (f"{_text(row.get('long_strike')) or 'n/a'}"
               f"/{_text(row.get('short_strike')) or 'n/a'}")
    reason = _text(row.get("exit_reason")) or "no exit reason"
    if not is_trade(row):
        return f"{direction} {strikes} no fill, {reason}"
    return (f"{direction} {strikes} entry {_text(row.get('fill_price')) or 'n/a'} "
            f"exit {_text(row.get('exit_fill')) or 'n/a'} "
            f"fees {_text(row.get('fees')) or 'n/a'}, {reason}")


def what_happened(rows: list[dict]) -> str:
    """``What happened`` for the day: every row the selected book has, or none."""
    if not rows:
        return NO_ROWS
    return "; ".join(row_facts(row) for row in rows) + "."


def sheet_row(day: date, rows: list[dict]) -> list[str]:
    """The one row this wrap upserts, in :data:`HEADER` order.

    ``Worst`` stays empty: the book has no worst or drawdown column and the
    runner stores no intraday mark, so there is nothing true to put there yet.
    """
    trades = [row for row in rows if is_trade(row)]
    return [
        day.isoformat(),
        BOI,
        "yes" if trades else "no",
        pnl_cell(trades),
        "",
        what_happened(rows),
    ]


# --------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------

def credentials_info(env=None) -> dict | None:
    """The service-account json straight from the environment, or ``None``.

    Whitespace-only is unset. A value that is set but is not a json object is a
    :class:`ValueError` for the caller: it does not quietly fall through to a key
    file, because a host that was handed a blob meant to use it. Nothing here
    writes the value anywhere and the exception carries no part of it.
    """
    raw = _text((os.environ if env is None else env).get(JSON_ENV))
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{JSON_ENV} is not valid json") from exc
    if not isinstance(info, dict):
        raise ValueError(f"{JSON_ENV} is not a json object")
    return info


def credentials_path(cfg) -> Path | None:
    """The key file, or ``None`` when the first name that was set is not there.

    config ``sheets_credentials``, then the two path variables, then the default
    under ``state/``. The first non-blank one is the answer: a path that was
    named and does not exist is a missing key, not a reason to try the next name.
    """
    raw = _text(getattr(cfg, "sheets_credentials", ""))
    if not raw:
        for name in PATH_ENVS:
            raw = _text(os.environ.get(name))
            if raw:
                break
    if not raw:
        raw = str(default_key_path(cfg))
    path = Path(raw).expanduser()
    return path if path.exists() else None


def default_key_path(cfg) -> Path:
    """``<home>/state/sheets-service-account.json``, the path the status line names."""
    return Path(cfg.state_dir) / KEY_NAME


# --------------------------------------------------------------------------
# The upsert
# --------------------------------------------------------------------------

def plan_sheet(existing: list[list[str]], incoming: list[list[str]]) -> tuple[dict, str]:
    """What to send to the sheet. Never includes a row this report does not own.

    ``create`` is a full header-plus-rows grid only when the tab is empty.
    Otherwise ``updates`` are ``(1-based row, values)`` for a ``(Date, Boi)``
    already on the tab and ``appends`` are the rest, so Candle's rows, Fly's
    rows, and a header this report did not write are not in the result at all
    and cannot be overwritten.
    """
    if not existing or not any(any(str(cell).strip() for cell in row) for row in existing):
        return {"create": [HEADER] + incoming, "updates": [], "appends": []}, "wrote a fresh table"
    header = [str(cell).strip() for cell in existing[0]]
    owned = header[:len(HEADER)] == HEADER
    index = {}
    for i, row in enumerate(existing):
        if i == 0 or len(row) < 2:
            continue
        index[(str(row[0]).strip(), str(row[1]).strip())] = i + 1
    updates, appends = [], []
    replaced = 0
    for inc in incoming:
        row_n = index.get((inc[0].strip(), inc[1].strip()))
        if row_n:
            updates.append((row_n, inc))
            replaced += 1
        else:
            appends.append(inc)
    if not owned:
        note = "the tab already had different column titles, so only this report's rows were added or updated"
    else:
        note = "updated today's rows" if replaced else "added today's rows"
    return {"create": None, "updates": updates, "appends": appends}, note


def push_sheet(cfg, tab: str, incoming: list[list[str]], client=None) -> str:
    """Upsert today's rows and return one status line. It does not raise.

    A missing key, an unparseable blob, the Google packages not being installed,
    and an API error are all one line and a wrap that still exits 0 with its
    markdown report on disk. The line names no secret: the blob is not quoted
    back, a key file's contents are never read into a message, and a failure
    carries the exception's type only.
    """
    if client is None:
        try:
            info = credentials_info()
        except ValueError:
            return (f"not added to the sheet: {JSON_ENV} is not valid service-account json. "
                    "Paste the whole service-account file, the outer braces included. "
                    "The value is not shown here.")
        try:
            if info is not None:
                client = _service_info(info)
            else:
                path = credentials_path(cfg)
                if path is None:
                    return ("not added to the sheet: no service-account key found. Put the json "
                            f"at {default_key_path(cfg)} (or name it in "
                            f"{', '.join(PATH_ENVS)}, or paste the whole file into {JSON_ENV}) "
                            "and share the sheet with that account.")
                client = _service_file(path)
        except ImportError:
            return ("not added to the sheet: the Google client libraries are not installed. "
                    'Run pip install -e ".[sheets]" and wrap again.')
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 — the markdown report still stands
            return ("not added to the sheet: the credential was found but a client could not be "
                    f"built from it ({type(exc).__name__}).")
    sheet_id = cfg.sheets_spreadsheet_id
    try:
        _ensure_tab(client, sheet_id, tab)
        existing = _read(client, sheet_id, tab)
        plan, how = plan_sheet(existing, incoming)
        _apply(client, sheet_id, tab, plan)
    except AssertionError:
        # The suite's blocked-socket guard is an AssertionError on purpose. A test
        # that would dial Google has to fail, not be swallowed into a status line.
        raise
    except Exception as exc:  # noqa: BLE001 — the markdown report still stands
        return f"not added to the {tab} tab: the sheet call failed ({type(exc).__name__})."
    return f"added to the {tab} tab ({how})."


# --------------------------------------------------------------------------
# The Google client. Imported here, not at module import, so the suite and a
# Mac without the extra never need the packages.
# --------------------------------------------------------------------------

def _build(creds):
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _service_file(path: Path):
    from google.oauth2 import service_account
    return _build(service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES))


def _service_info(info: dict):
    """Credentials from the blob in the environment. Nothing is written to disk."""
    from google.oauth2 import service_account
    return _build(service_account.Credentials.from_service_account_info(info, scopes=SCOPES))


def _ensure_tab(client, sheet_id: str, tab: str) -> None:
    meta = client.spreadsheets().get(spreadsheetId=sheet_id).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab in titles:
        return
    client.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={
        "requests": [{"addSheet": {"properties": {"title": tab}}}]
    }).execute()


def _a1(tab: str, cells: str) -> str:
    """A quoted A1 range. A tab title may hold an apostrophe (Tod's Comp): A1 doubles it."""
    return "'" + tab.replace("'", "''") + f"'!{cells}"


def _read(client, sheet_id: str, tab: str) -> list[list[str]]:
    resp = client.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=_a1(tab, "A:F")).execute()
    return resp.get("values", [])


def _apply(client, sheet_id: str, tab: str, plan: dict) -> None:
    """Write only the cells this report owns. An empty tab gets the header once."""
    if plan["create"] is not None:
        client.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=_a1(tab, "A1"),
            valueInputOption="RAW", body={"values": plan["create"]}).execute()
        return
    data = [{"range": _a1(tab, f"A{row_n}:F{row_n}"), "values": [row]}
            for row_n, row in plan["updates"]]
    if data:
        client.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": data}).execute()
    if plan["appends"]:
        client.spreadsheets().values().append(
            spreadsheetId=sheet_id, range=_a1(tab, "A:F"),
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": plan["appends"]}).execute()
