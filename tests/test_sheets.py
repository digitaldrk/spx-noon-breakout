"""The 16:20 wrap's one row on the shared Google Sheet.

Nothing here imports ``google.oauth2`` or ``googleapiclient``, installs the
``sheets`` extra, or opens a socket. The success path is driven by ``FakeSheets``
below, an in-memory stand-in for the Sheets v4 client, and every test that
exercises a *failure* path stubs the two client builders so that reaching for a
real credential, or for Google, fails the test instead of being swallowed into a
status line.

No credential value is printed either: the tests plant an obvious fake
``private_key`` and assert it is absent from the status, the report, and the
captured output.
"""
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from spx_noon import sheetsink
from spx_noon.config import Config
from spx_noon.logbook import Outputs
from spx_noon.sheetsink import (
    BOI,
    HEADER,
    JSON_ENV,
    NO_ROWS,
    credentials_info,
    credentials_path,
    default_key_path,
    machine_tab,
    plan_sheet,
    push_sheet,
    sheet_row,
)
from spx_noon.wrap_run import main as wrap_main

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 20)
TAB = "Derek Comp"

#: An obvious fake, planted where a real key would be. No test may echo it.
PLANTED_KEY = "-----BEGIN PRIVATE KEY-----AAAAFAKEKEYBBBB-----END PRIVATE KEY-----"
BLOB = json.dumps({
    "type": "service_account",
    "project_id": "noon-test",
    "client_email": "noon@example.iam.gserviceaccount.com",
    "private_key": PLANTED_KEY,
})

#: The issue's closed trade: a CALL vertical taken at 12.71 and closed at 16.52.
CLOSED = {
    "date": DAY, "timestamp": datetime(2026, 11, 20, 12, 15, tzinfo=ET),
    "direction": "CALL", "long_strike": 6950, "short_strike": 6980,
    "spread_mid": 12.70, "fill_price": 12.71, "slippage_per_side": 0.005,
    "exit_time": datetime(2026, 11, 20, 14, 15, tzinfo=ET), "exit_reason": "target",
    "exit_fill": 16.52, "pnl": 378.70, "fees": 2.60, "mode": "paper",
}
SKIPPED = {
    "date": DAY, "direction": "PUT", "long_strike": 6900, "short_strike": 6870,
    "exit_reason": "skip_no_fill", "mode": "paper",
}


def _cfg(tmp_path, **extra):
    data = dict(broker="ibkr", mode="dry", base_dir=str(tmp_path))
    data.update(extra)
    cfg = Config(**data)
    cfg.validate()
    cfg.ensure_dirs()
    return cfg


def _book(tmp_path, *rows, book="paper"):
    """Rows written the way the runner writes them, then read back as the wrap reads them."""
    out = Outputs(Path(tmp_path) / "state", book=book)
    for row in rows:
        out.append_trade(row)
    return out.read_trades(DAY)


def _no_google(monkeypatch):
    """Both client builders fail the test if anything reaches for a credential."""
    def refuse(*_args, **_kwargs):
        pytest.fail("a test built a Google client; it would have opened a socket")

    monkeypatch.setattr(sheetsink, "_service_info", refuse)
    monkeypatch.setattr(sheetsink, "_service_file", refuse)


# --------------------------------------------------------------------------
# The row
# --------------------------------------------------------------------------

def test_a_closed_trade_is_one_traded_row_with_the_books_own_pnl(tmp_path):
    rows = _book(tmp_path, CLOSED)
    row = sheet_row(DAY, rows)
    assert row[:3] == [DAY.isoformat(), BOI, "yes"]
    assert len(row) == len(HEADER)
    # the book's pnl is already net of fees (pricing.trade_pnl); it is not re-netted
    assert row[3] == rows[0]["pnl"] == "378.7"
    assert row[4] == ""                                   # Worst has no source yet
    for fact in ("CALL", "6950/6980", "entry 12.71", "exit 16.52", "fees 2.6", "target"):
        assert fact in row[5], fact


def test_a_day_with_no_rows_is_traded_no_and_says_so(tmp_path):
    row = sheet_row(DAY, _book(tmp_path))
    assert row == [DAY.isoformat(), BOI, "no", "", "", NO_ROWS]


def test_an_unfilled_day_is_traded_no_and_still_carries_the_facts(tmp_path):
    for skip in ("skip_no_fill", "skip_morning"):
        rows = _book(tmp_path / skip, {**SKIPPED, "exit_reason": skip})
        row = sheet_row(DAY, rows)
        assert row[2] == "no" and row[3] == "" and row[4] == ""
        for fact in ("PUT", "6900/6870", skip):
            assert fact in row[5], fact
        assert row[5] != NO_ROWS


def test_a_fill_and_a_skip_on_the_same_day_stay_traded_yes(tmp_path):
    rows = _book(tmp_path, CLOSED, SKIPPED)
    row = sheet_row(DAY, rows)
    assert row[2] == "yes"
    assert row[3] == "378.7"                   # the skip is not a zero in the total
    assert "skip_no_fill" in row[5] and "target" in row[5]


def test_two_traded_rows_add_up_and_a_blank_pnl_leaves_the_cell_empty(tmp_path):
    both = _book(tmp_path / "two", CLOSED, {**CLOSED, "pnl": -100.05, "exit_reason": "time"})
    assert sheet_row(DAY, both)[3] == "278.65"
    # a filled row the runner has not closed yet: traded, but there is no number
    open_row = _book(tmp_path / "open", {**CLOSED, "pnl": None, "exit_fill": None})
    assert sheet_row(DAY, open_row)[2] == "yes"
    assert sheet_row(DAY, open_row)[3] == ""
    mixed = _book(tmp_path / "mixed", CLOSED, {**CLOSED, "pnl": None, "exit_fill": None})
    assert sheet_row(DAY, mixed)[2] == "yes" and sheet_row(DAY, mixed)[3] == ""


def test_a_sum_of_two_rows_rounds_half_a_cent_away_from_zero(tmp_path):
    """The book keeps four decimals, so the sum has to be decimal, not binary.

    ``2.675 + 0`` is ``2.67`` through ``float`` and ``2.68`` through ``Decimal``.
    """
    cases = (
        (("2.675", "0"), "2.68"),
        (("-2.675", "0"), "-2.68"),
        (("0.005", "0.005"), "0.01"),
        (("378.7", "-100.05"), "278.65"),
        (("-0.004", "0"), "0.00"),      # a rounded-away sign is not a negative zero
        (("1.0001", "2.0001"), "3.00"),
    )
    for values, want in cases:
        trades = [{"pnl": value, "fill_price": "1"} for value in values]
        assert sheetsink.pnl_cell(trades) == want, values
    # one row is its own string, untouched, however many places it has
    assert sheetsink.pnl_cell([{"pnl": "2.675"}]) == "2.675"
    # and nothing numeric-looking is invented out of a value that will not parse
    for bad in ("n/a", "", "   ", "1,200.00", "$5"):
        assert sheetsink.pnl_cell([{"pnl": bad}, {"pnl": "1.00"}]) == "", bad
    for wild in ("nan", "inf", "-inf"):
        assert sheetsink.pnl_cell([{"pnl": wild}, {"pnl": "1.00"}]) == "", wild
    # through the real book, four decimals and all
    rows = _book(tmp_path, {**CLOSED, "pnl": 2.675}, {**CLOSED, "pnl": 0.0})
    assert [row["pnl"] for row in rows] == ["2.675", "0"]
    assert sheet_row(DAY, rows)[3] == "2.68"


def test_the_tab_follows_the_operating_system_and_nothing_else():
    assert machine_tab("darwin") == TAB
    assert machine_tab("linux") == TAB            # Render writes Derek's tab too
    assert machine_tab("win32") == "Tod Comp"
    assert machine_tab("freebsd13") == TAB


def test_the_tab_is_the_only_switch_there_is(monkeypatch):
    """No ``sheets_tab`` config field and no tab variable."""
    assert not hasattr(Config(broker="ibkr", mode="dry"), "sheets_tab")
    monkeypatch.setattr(sys, "platform", "win32")
    assert machine_tab() == "Tod Comp"
    monkeypatch.setattr(sys, "platform", "linux")
    assert machine_tab() == TAB


# --------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------

def test_the_credential_order_is_blob_then_config_then_env_then_the_default(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    planted = tmp_path / "state" / sheetsink.KEY_NAME
    planted.write_text(BLOB)
    named = tmp_path / "named.json"
    named.write_text(BLOB)
    env_path = tmp_path / "from-env.json"
    env_path.write_text(BLOB)
    google_path = tmp_path / "from-google.json"
    google_path.write_text(BLOB)

    # the default path, with nothing set
    assert credentials_path(cfg) == planted == default_key_path(cfg)
    assert credentials_info() is None

    # GOOGLE_APPLICATION_CREDENTIALS, then SPX_SHEETS_CREDENTIALS over it
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(google_path))
    assert credentials_path(cfg) == google_path
    monkeypatch.setenv("SPX_SHEETS_CREDENTIALS", str(env_path))
    assert credentials_path(cfg) == env_path
    # config sheets_credentials over both
    assert credentials_path(_cfg(tmp_path, sheets_credentials=str(named))) == named
    assert credentials_path(_cfg(tmp_path, sheets_credentials="   ")) == env_path

    # the blob wins over a file that is right there, and is not written anywhere
    monkeypatch.setenv(JSON_ENV, BLOB)
    assert credentials_info()["client_email"] == "noon@example.iam.gserviceaccount.com"
    assert sorted(p.name for p in (tmp_path / "state").iterdir()) == [sheetsink.KEY_NAME]
    assert (tmp_path / "state" / sheetsink.KEY_NAME).read_text() == BLOB

    # whitespace-only is unset, and a named path that is not there is a missing key
    for blank in ("", "   ", "\n"):
        monkeypatch.setenv(JSON_ENV, blank)
        assert credentials_info() is None
    assert credentials_path(_cfg(tmp_path, sheets_credentials=str(tmp_path / "gone.json"))) is None


def test_a_bad_blob_does_not_fall_through_to_a_file_and_is_never_echoed(monkeypatch, tmp_path):
    _no_google(monkeypatch)
    cfg = _cfg(tmp_path)
    (tmp_path / "state" / sheetsink.KEY_NAME).write_text(BLOB)   # a usable file is right there
    for bad in ("{not json", f'"{PLANTED_KEY}"', "[]", "null", "17", BLOB[:40]):
        monkeypatch.setenv(JSON_ENV, bad)
        with pytest.raises(ValueError, match=JSON_ENV):
            credentials_info()
        status = push_sheet(cfg, TAB, [sheet_row(DAY, [])])
        assert status.startswith(f"not added to the sheet: {JSON_ENV} is not valid")
        assert "not shown" in status
        assert PLANTED_KEY not in status and bad not in status
        assert "private_key" not in status


def test_no_key_at_all_names_the_path_and_the_variables_and_reads_no_file(monkeypatch, tmp_path):
    _no_google(monkeypatch)
    cfg = _cfg(tmp_path)
    assert not default_key_path(cfg).exists()
    status = push_sheet(cfg, TAB, [sheet_row(DAY, [])])
    assert status.startswith("not added to the sheet: no service-account key found")
    assert str(default_key_path(cfg)) in status
    for name in (JSON_ENV, "SPX_SHEETS_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS"):
        assert name in status
    for claim in ("read", "parsed"):
        assert claim not in status


def test_a_missing_google_library_says_which_extra_to_install(monkeypatch, tmp_path):
    monkeypatch.setenv(JSON_ENV, BLOB)

    def missing(*_args, **_kwargs):
        raise ImportError("No module named 'google'")

    monkeypatch.setattr(sheetsink, "_service_info", missing)
    status = push_sheet(_cfg(tmp_path), TAB, [sheet_row(DAY, [])])
    assert "not added to the sheet" in status
    assert '".[sheets]"' in status
    assert PLANTED_KEY not in status


def test_a_credential_that_will_not_build_reports_its_type_only(monkeypatch, tmp_path):
    monkeypatch.setenv(JSON_ENV, BLOB)

    class KeyRejected(ValueError):
        pass

    def refuse(*_args, **_kwargs):
        raise KeyRejected(f"could not deserialize {PLANTED_KEY}")

    monkeypatch.setattr(sheetsink, "_service_info", refuse)
    status = push_sheet(_cfg(tmp_path), TAB, [sheet_row(DAY, [])])
    assert "(KeyRejected)" in status
    assert PLANTED_KEY not in status and "deserialize" not in status


# --------------------------------------------------------------------------
# An in-memory Sheets client
# --------------------------------------------------------------------------

class _Call:
    def __init__(self, run):
        self._run = run

    def execute(self):
        return self._run()


class _Values:
    def __init__(self, sheet):
        self.sheet = sheet

    def get(self, spreadsheetId=None, range=None):
        self.sheet.seen(spreadsheetId, range)
        return _Call(lambda: {"values": [list(row) for row in self.sheet.grid]})

    def update(self, spreadsheetId=None, range=None, valueInputOption=None, body=None):
        self.sheet.seen(spreadsheetId, range)
        assert valueInputOption == "RAW"
        return _Call(lambda: self.sheet.write(range, body["values"]))

    def batchUpdate(self, spreadsheetId=None, body=None):
        assert body["valueInputOption"] == "RAW"
        for item in body["data"]:
            self.sheet.seen(spreadsheetId, item["range"])
        return _Call(lambda: [self.sheet.write(item["range"], item["values"])
                              for item in body["data"]])

    def append(self, spreadsheetId=None, range=None, valueInputOption=None,
               insertDataOption=None, body=None):
        self.sheet.seen(spreadsheetId, range)
        assert insertDataOption == "INSERT_ROWS"
        return _Call(lambda: self.sheet.grid.extend(list(row) for row in body["values"]))


class _Spreadsheets:
    def __init__(self, sheet):
        self.sheet = sheet

    def get(self, spreadsheetId=None):
        self.sheet.seen(spreadsheetId, None)
        return _Call(lambda: {"sheets": [{"properties": {"title": title}}
                                         for title in self.sheet.titles]})

    def batchUpdate(self, spreadsheetId=None, body=None):
        self.sheet.seen(spreadsheetId, None)
        titles = [request["addSheet"]["properties"]["title"]
                  for request in body["requests"]]
        return _Call(lambda: self.sheet.titles.extend(titles))

    def values(self):
        return _Values(self.sheet)


class FakeSheets:
    """The slice of the Sheets v4 client :mod:`spx_noon.sheetsink` uses.

    It keeps one tab's grid in memory and applies exactly the writes it is sent,
    so a row outside a written range stays byte for byte what it was.
    """

    def __init__(self, grid=(), titles=(TAB,)):
        self.grid = [list(row) for row in grid]
        self.titles = list(titles)
        self.ranges = []
        self.ids = []

    def spreadsheets(self):
        return _Spreadsheets(self)

    def seen(self, sheet_id, a1):
        self.ids.append(sheet_id)
        if a1 is not None:
            self.ranges.append(a1)

    def write(self, a1, values):
        first = int(re.search(r"!A(\d+)", a1).group(1))
        while len(self.grid) < first - 1 + len(values):
            self.grid.append([])
        for offset, row in enumerate(values):
            self.grid[first - 1 + offset] = list(row)


CANDLE = ["2026-11-20", "Candle", "yes", "412.00", "-88.00", "broke 9:45, target at 11:02."]
FLY = ["2026-11-20", "Fly", "no", "", "", "no fly today."]


def _tab(*rows):
    return FakeSheets(grid=[list(HEADER), *rows])


# --------------------------------------------------------------------------
# The plan and the push
# --------------------------------------------------------------------------

def test_an_empty_tab_gets_the_header_once_and_nothing_else(tmp_path):
    client = FakeSheets()
    row = sheet_row(DAY, _book(tmp_path, CLOSED))
    assert push_sheet(_cfg(tmp_path), TAB, [row], client=client) == \
        f"added to the {TAB} tab (wrote a fresh table)."
    assert client.grid == [HEADER, row]
    assert client.ids and set(client.ids) == {_cfg(tmp_path).sheets_spreadsheet_id}
    assert client.ids[0] == "1tnMlDTidwCYG3_H2Z-lBcIUG5KOX4tgbe8AiRopATsU"


def test_candle_and_fly_rows_are_untouched_and_high_noon_is_added_once(tmp_path):
    client = _tab(CANDLE, FLY)
    cfg = _cfg(tmp_path)
    row = sheet_row(DAY, _book(tmp_path, CLOSED))
    assert push_sheet(cfg, TAB, [row], client=client) == \
        f"added to the {TAB} tab (added today's rows)."
    assert client.grid == [HEADER, CANDLE, FLY, row]

    # a second wrap for the same date replaces that one row in place
    later = sheet_row(DAY, _book(tmp_path, CLOSED, {**CLOSED, "pnl": 21.30, "exit_reason": "time"}))
    assert later != row
    assert push_sheet(cfg, TAB, [later], client=client) == \
        f"added to the {TAB} tab (updated today's rows)."
    assert client.grid == [HEADER, CANDLE, FLY, later]
    assert len([line for line in client.grid if line[1:2] == [BOI]]) == 1
    assert client.grid[1] == CANDLE and client.grid[2] == FLY
    assert any(a1.startswith(f"'{TAB}'!A4:F4") for a1 in client.ranges), client.ranges


def test_a_header_this_report_does_not_own_is_left_alone(tmp_path):
    theirs = ["When", "Who", "Did it trade", "Money", "Low", "Story"]
    client = FakeSheets(grid=[theirs, CANDLE])
    row = sheet_row(DAY, _book(tmp_path, CLOSED))
    status = push_sheet(_cfg(tmp_path), TAB, [row], client=client)
    assert "different column titles" in status
    assert client.grid == [theirs, CANDLE, row]


def test_the_plan_never_names_a_row_it_does_not_own():
    row = [DAY.isoformat(), BOI, "no", "", "", NO_ROWS]
    plan, how = plan_sheet([HEADER, CANDLE, FLY], [row])
    assert plan == {"create": None, "updates": [], "appends": [row]}
    assert how == "added today's rows"
    plan, how = plan_sheet([HEADER, CANDLE, [DAY.isoformat(), BOI, "yes"], FLY], [row])
    assert plan == {"create": None, "updates": [(3, row)], "appends": []}
    assert how == "updated today's rows"
    # a short row and a blank grid are not keys to overwrite
    plan, _how = plan_sheet([HEADER, ["2026-11-20"], []], [row])
    assert plan["updates"] == [] and plan["appends"] == [row]
    plan, how = plan_sheet([[], ["", "  "]], [row])
    assert plan["create"] == [HEADER, row] and how == "wrote a fresh table"


def test_a_tab_that_is_not_there_yet_is_created_before_the_write(tmp_path):
    client = FakeSheets(titles=["Candle", "Fly"])
    row = sheet_row(DAY, _book(tmp_path, CLOSED))
    assert "added to the" in push_sheet(_cfg(tmp_path), TAB, [row], client=client)
    assert client.titles == ["Candle", "Fly", TAB]
    assert client.grid == [HEADER, row]


def test_a_tab_name_with_an_apostrophe_is_quoted():
    assert sheetsink._a1("Tod's Comp", "A:F") == "'Tod''s Comp'!A:F"
    assert sheetsink._a1(TAB, "A4:F4") == "'Derek Comp'!A4:F4"


def test_an_api_error_is_one_line_with_the_exception_type_only(tmp_path):
    class HttpError(RuntimeError):
        pass

    class Angry(FakeSheets):
        def spreadsheets(self):
            raise HttpError(f"403 caller has no access; key {PLANTED_KEY}")

    status = push_sheet(_cfg(tmp_path), TAB, [sheet_row(DAY, [])], client=Angry())
    assert status == f"not added to the {TAB} tab: the sheet call failed (HttpError)."
    assert PLANTED_KEY not in status and "403" not in status


def test_a_blocked_socket_is_not_swallowed_into_a_status_line(tmp_path):
    """``conftest.BlockedConnection`` is an ``AssertionError`` so that a test which
    would really dial Google fails instead of reading like a quiet sheet error."""
    from conftest import BlockedConnection

    class Blocked(FakeSheets):
        def spreadsheets(self):
            raise BlockedConnection("test tried to reach sheets.googleapis.com")

    with pytest.raises(BlockedConnection):
        push_sheet(_cfg(tmp_path), TAB, [sheet_row(DAY, [])], client=Blocked())


# --------------------------------------------------------------------------
# The wrap end to end
# --------------------------------------------------------------------------

def _wrap_config(tmp_path, **keys):
    data = {"broker": "ibkr", "ibkr_port": 7497, "mode": "dry", "base_dir": str(tmp_path)}
    data.update(keys)
    path = Path(tmp_path) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_a_wrap_with_no_key_still_writes_the_report_and_exits_zero(
        monkeypatch, tmp_path, capsys):
    _no_google(monkeypatch)
    path = _wrap_config(tmp_path)
    today = datetime.now(ET).date()
    Outputs(tmp_path / "state", book="paper").append_trade({**CLOSED, "date": today})
    assert wrap_main(["--config", str(path), "--now"]) == 0
    report = tmp_path / "reports" / f"wrap-{today.isoformat()}.md"
    text = report.read_text()
    assert "no service-account key found" in text
    assert "Source: state/trades.csv" in text and "CALL" in text   # the body is unchanged
    out = capsys.readouterr()
    assert f"wrote {report}" in out.out
    assert "no service-account key found" in out.out
    for leak in (PLANTED_KEY, "private_key", "BEGIN PRIVATE KEY"):
        assert leak not in text and leak not in out.out and leak not in out.err


def test_a_planted_blob_never_reaches_the_report_or_the_output(monkeypatch, tmp_path, capsys):
    """A blob that is set and that Google refuses: the whole value is in the
    exception, and none of it may reach the report, stdout, stderr, or the disk."""
    monkeypatch.setenv(JSON_ENV, BLOB)

    def refuse(info):
        assert info["private_key"] == PLANTED_KEY        # it did read the blob
        raise ValueError(f"bad service account: {BLOB}")

    monkeypatch.setattr(sheetsink, "_service_info", refuse)
    path = _wrap_config(tmp_path)
    today = datetime.now(ET).date()
    assert wrap_main(["--config", str(path), "--now"]) == 0
    text = (tmp_path / "reports" / f"wrap-{today.isoformat()}.md").read_text()
    out = capsys.readouterr()
    assert NO_ROWS in text                               # the markdown body, as before
    assert "Sheet:" in text
    for leak in (PLANTED_KEY, "private_key", BLOB):
        assert leak not in text and leak not in out.out and leak not in out.err
    # and nothing wrote the blob to disk
    for folder in ("state", "reports"):
        for child in (tmp_path / folder).iterdir():
            assert PLANTED_KEY not in child.read_text()


def test_the_wrap_upserts_one_high_noon_row_for_the_book_it_chose(monkeypatch, tmp_path, capsys):
    """Acceptance 1: the markdown report and one row, with the issue's facts."""
    client = _tab(CANDLE, FLY)
    monkeypatch.setattr(sheetsink, "machine_tab", lambda platform=None: TAB)
    monkeypatch.setattr("spx_noon.wrap_run.machine_tab", lambda platform=None: TAB)
    monkeypatch.setattr("spx_noon.wrap_run.push_sheet",
                        lambda cfg, tab, incoming: sheetsink.push_sheet(
                            cfg, tab, incoming, client=client))
    path = _wrap_config(tmp_path)
    today = datetime.now(ET).date()
    Outputs(tmp_path / "state", book="paper").append_trade({**CLOSED, "date": today})
    assert wrap_main(["--config", str(path), "--now"]) == 0

    assert client.grid[1] == CANDLE and client.grid[2] == FLY
    row = client.grid[3]
    assert row[0] == today.isoformat() and row[1] == BOI and row[2] == "yes"
    assert row[3] == "378.7" and row[4] == ""
    for fact in ("CALL", "6950/6980", "entry 12.71", "exit 16.52", "fees 2.6", "target"):
        assert fact in row[5], fact
    assert f"added to the {TAB} tab" in capsys.readouterr().out
    assert len(client.grid) == 4                          # no duplicate, no second tab row

    # a second wrap for the same date updates that row rather than adding one
    assert wrap_main(["--config", str(path), "--now"]) == 0
    assert len(client.grid) == 4
    assert client.grid[3][1] == BOI


def test_an_unfilled_day_and_an_empty_day_both_get_a_row(monkeypatch, tmp_path):
    """Acceptance 2: the wrap does not skip the row when there was no trade."""
    for name, rows in (("empty", ()), ("skip", (SKIPPED,))):
        base = tmp_path / name
        client = _tab(CANDLE)
        monkeypatch.setattr("spx_noon.wrap_run.machine_tab", lambda platform=None: TAB)
        monkeypatch.setattr("spx_noon.wrap_run.push_sheet",
                            lambda cfg, tab, incoming: sheetsink.push_sheet(
                                cfg, tab, incoming, client=client))
        path = _wrap_config(base)
        today = datetime.now(ET).date()
        for row in rows:
            Outputs(base / "state", book="paper").append_trade({**row, "date": today})
        assert wrap_main(["--config", str(path), "--now"]) == 0
        assert len(client.grid) == 3
        written = client.grid[2]
        assert written[:3] == [today.isoformat(), BOI, "no"]
        assert written[3] == "" and written[4] == ""
        assert written[5] == (NO_ROWS if not rows else written[5])
        if rows:
            assert "skip_no_fill" in written[5] and "6900/6870" in written[5]


def test_the_row_is_the_book_the_wrap_chose_and_never_the_other_one(monkeypatch, tmp_path):
    """One book, the one ``wrap_run.main`` already selects, with both books filled.

    An IBKR live config writes the live book's numbers; the same config with
    ``--book paper`` writes the paper book's. Both are ``High Noon``, so the
    second wrap replaces the first row rather than adding a second.
    """
    client = _tab(CANDLE, FLY)
    monkeypatch.setattr("spx_noon.wrap_run.machine_tab", lambda platform=None: TAB)
    monkeypatch.setattr("spx_noon.wrap_run.push_sheet",
                        lambda cfg, tab, incoming: sheetsink.push_sheet(
                            cfg, tab, incoming, client=client))
    path = _wrap_config(tmp_path, mode="live", ibkr_port=7496, broker_account="U7654321")
    today = datetime.now(ET).date()
    Outputs(tmp_path / "state", book="paper").append_trade(
        {**CLOSED, "date": today, "pnl": 11.11, "direction": "CALL", "mode": "paper"})
    Outputs(tmp_path / "state", book="live").append_trade(
        {**CLOSED, "date": today, "pnl": -22.22, "direction": "PUT", "mode": "live"})

    assert wrap_main(["--config", str(path), "--now"]) == 0     # no --book: live config
    assert len(client.grid) == 4
    live_row = client.grid[3]
    assert live_row[1] == BOI and live_row[3] == "-22.22"
    assert "PUT" in live_row[5] and "CALL" not in live_row[5]

    assert wrap_main(["--config", str(path), "--now", "--book", "paper"]) == 0
    assert len(client.grid) == 4                               # the one row, replaced
    paper_row = client.grid[3]
    assert paper_row[1] == BOI and paper_row[3] == "11.11"
    assert "CALL" in paper_row[5] and "PUT" not in paper_row[5]
    assert client.grid[1] == CANDLE and client.grid[2] == FLY
    # the two markdown files stay separate, as they do today
    assert (tmp_path / "reports" / f"wrap-live-{today.isoformat()}.md").exists()
    assert (tmp_path / "reports" / f"wrap-{today.isoformat()}.md").exists()


def test_the_sheet_status_does_not_leak_the_other_book(monkeypatch, tmp_path):
    """The status line is about the sheet. It repeats no trade, so the paper and
    live reports still cannot see each other's rows (test_session pins that too)."""
    _no_google(monkeypatch)
    path = _wrap_config(tmp_path)
    today = datetime.now(ET).date()
    Outputs(tmp_path / "state", book="paper").append_trade(
        {"date": today, "direction": "CALL", "exit_reason": "target", "mode": "paper"})
    Outputs(tmp_path / "state", book="live").append_trade(
        {"date": today, "direction": "PUT", "exit_reason": "time", "mode": "live"})
    assert wrap_main(["--config", str(path), "--now"]) == 0
    paper = (tmp_path / "reports" / f"wrap-{today.isoformat()}.md").read_text()
    assert "Sheet: not added" in paper
    assert "PUT" not in paper and "trades-live.csv" not in paper


def test_a_bad_config_is_still_exit_two_and_touches_nothing(monkeypatch, tmp_path, capsys):
    _no_google(monkeypatch)
    path = _wrap_config(tmp_path, broker="")
    assert wrap_main(["--config", str(path), "--now"]) == 2
    assert "refusing to wrap" in capsys.readouterr().out
    assert not (tmp_path / "reports").exists()


def test_every_sheet_failure_keeps_the_report_prints_a_line_and_exits_zero(
        monkeypatch, tmp_path, capsys):
    """Acceptance 5, the four ways the sheet can fail, each through the real wrap."""
    class Angry(FakeSheets):
        def spreadsheets(self):
            raise RuntimeError(f"500 backend error {PLANTED_KEY}")

    def missing(*_args, **_kwargs):
        raise ImportError("No module named 'googleapiclient'")

    def no_key():
        _no_google(monkeypatch)

    def bad_blob():
        _no_google(monkeypatch)
        monkeypatch.setenv(JSON_ENV, f'{{"private_key": "{PLANTED_KEY}"')

    def no_library():
        monkeypatch.setenv(JSON_ENV, BLOB)
        monkeypatch.setattr(sheetsink, "_service_info", missing)

    def push_fails():
        monkeypatch.setattr("spx_noon.wrap_run.push_sheet",
                            lambda cfg, tab, incoming: sheetsink.push_sheet(
                                cfg, tab, incoming, client=Angry()))

    cases = {
        "no-key": (no_key, "no service-account key found"),
        "bad-blob": (bad_blob, f"{JSON_ENV} is not valid service-account json"),
        "no-library": (no_library, '".[sheets]"'),
        "push-fails": (push_fails, "the sheet call failed (RuntimeError)"),
    }
    today = datetime.now(ET).date()
    for name, (setup, needle) in cases.items():
        base = tmp_path / name
        setup()
        path = _wrap_config(base)
        Outputs(base / "state", book="paper").append_trade({**CLOSED, "date": today})
        assert wrap_main(["--config", str(path), "--now"]) == 0, name
        report = base / "reports" / f"wrap-{today.isoformat()}.md"
        text = report.read_text()
        out = capsys.readouterr()
        assert "entry 12.71" not in text                   # the body, not the sheet row
        assert "CALL 6950/6980" in text and "pnl 378.7" in text
        assert needle in text and needle in out.out, name
        assert f"wrote {report}" in out.out, name
        for leak in (PLANTED_KEY, "private_key", "BEGIN PRIVATE KEY"):
            assert leak not in text and leak not in out.out and leak not in out.err


def test_the_default_spreadsheet_id_is_the_shared_one_and_the_key_is_not_a_config_field():
    cfg = Config(broker="ibkr", mode="dry")
    assert cfg.sheets_spreadsheet_id == "1tnMlDTidwCYG3_H2Z-lBcIUG5KOX4tgbe8AiRopATsU"
    assert cfg.sheets_credentials == ""
    assert JSON_ENV not in json.dumps(cfg.to_dict())      # publish_config cannot carry it
    for name in ("sheets_credentials_json", "sheets_tab", "sheets_key"):
        assert name not in cfg.to_dict()


def test_the_docs_and_the_examples_carry_the_path_the_variables_and_the_tab_rule():
    root = Path(__file__).resolve().parent.parent
    default_id = "1tnMlDTidwCYG3_H2Z-lBcIUG5KOX4tgbe8AiRopATsU"
    for name in ("README.md", "RENDER.md"):
        text = (root / name).read_text()
        assert default_id in text, name
        assert "sheets-service-account.json" in text, name
        assert "Derek Comp" in text and "Tod Comp" in text, name
        assert '".[sheets]"' in text, name
        assert JSON_ENV in text, name
        for variable in ("SPX_SHEETS_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS"):
            assert variable in text, (name, variable)
    for name in ("config.example.json", "config.tasty.example.json",
                 "config.render.example.json"):
        data = json.loads((root / name).read_text())
        assert data["sheets_spreadsheet_id"] == default_id, name
        assert data["sheets_credentials"] == "", name
        assert "High Noon" in data["_sheets"], name
    # the Render seed names no secret of its own, this one included
    seed = (root / "config.render.example.json").read_text()
    assert JSON_ENV not in seed and "six secrets" in seed
    assert "the five secrets" not in seed


def test_before_sixteen_hundred_without_now_the_sheet_is_not_touched(monkeypatch, tmp_path, capsys):
    _no_google(monkeypatch)
    monkeypatch.setattr("spx_noon.wrap_run.push_sheet",
                        lambda *args, **kwargs: pytest.fail("an early wrap touched the sheet"))
    path = _wrap_config(tmp_path)

    class Early(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 20, 9, 30, tzinfo=tz)

    monkeypatch.setattr("spx_noon.wrap_run.datetime", Early)
    assert wrap_main(["--config", str(path)]) == 0
    assert list((tmp_path / "reports").iterdir()) == []
    assert "Wrap waits until 16:00 ET" in capsys.readouterr().out
