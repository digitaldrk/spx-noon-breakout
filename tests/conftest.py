"""The suite ignores this machine's broker setup.

Every test runs as if the machine had never been set up for a broker: no
``TASTYTRADE_*`` credentials, no ``~/secrets``, no ``config.json``, no state
directory of the operator's, and no route to tastytrade or TWS. A machine with
IBKR installed, a machine with tastytrade credentials exported, and a machine
with neither must all give the same passed, failed, and skipped counts.

Nothing here changes production behaviour. ``sim`` is the in-process fake the
tests pass as ``Config(broker="sim")``. A test that wants credentials or a
config file sets them itself, after this fixture, as the tastytrade tests already do.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

#: Variables the Mac launchers and ``spx_noon.tasty`` read. Cleared
#: by prefix, so a credential name added later is covered without an edit here.
PREFIXES = ("TASTYTRADE_", "SPX_NOON_")

#: The hosted worker's own plain names, from :mod:`spx_noon.service`. Cleared by
#: exact name: ``DATA`` and ``MODE`` are too common to clear by prefix.
WORKER_NAMES = ("DATA", "BROKER", "MODE", "TASTY_DATA", "TASTY_ORDERS", "BROKER_ACCOUNT")

#: The Google Sheets credential, in the three forms :mod:`spx_noon.sheetsink`
#: looks for. A developer shell that exports any of them would otherwise let a
#: wrap test read the operator's real service-account key and dial Google, so
#: all three are cleared: the blob, the repo's own path variable, and the
#: library-wide one.
OTHER_NAMES = ("SPX_SHEETS_CREDENTIALS_JSON", "SPX_SHEETS_CREDENTIALS",
               "GOOGLE_APPLICATION_CREDENTIALS")

#: The three names the fixture replaces instead of removing: a machine whose
#: shell exports one of them (``run.sh`` exports ``SPX_NOON_HOME``) still has it
#: inside a test, pointing at the isolated path. Everything else in ``PREFIXES``
#: is gone.
REPLACED_NAMES = ("TASTYTRADE_CRED_DIR", "SPX_NOON_HOME", "SPX_NOON_CONFIG")

#: Taken at import, before any fixture has run: the names that were in the
#: environment the process started with, never their values. The proof test in
#: ``test_isolation.py`` asserts each one is gone inside a test. Printing a name
#: is safe; printing a value would put a credential in the pytest output.
PRESENT_AT_IMPORT = tuple(sorted(name for name in os.environ if name.startswith(PREFIXES)))

#: The home the process started with, for the same proof: the isolated
#: credential directory must not be the operator's ``$HOME/secrets``.
ORIGINAL_HOME = os.environ.get("HOME", "")

#: A tastytrade API host, by name. After DNS the connect argument is an IP, so
#: the name is matched wherever it can still be seen: ``create_connection`` and
#: the address lookup itself.
TASTY_DOMAINS = ("tastyworks.com", "tastytrade.com")

#: TWS and IB Gateway. The paper port, the live port, and the two gateway ports.
TWS_PORTS = frozenset({7496, 7497, 4001, 4002})

#: Only these names are treated as this machine. Every other localhost port is
#: left open: pytest, subprocess, and the socket tests below need them.
LOCALHOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class BlockedConnection(AssertionError):
    """A test tried to reach a real broker.

    An ``AssertionError`` rather than an ``OSError`` on purpose: code under test
    that retries a refused connection must not swallow this. Use the fakes
    instead: ``SimBroker``, the ``Sess`` and ``Transport`` doubles in
    ``test_tasty.py``, or ``_FakeIB`` in ``test_safety.py``.
    """


def _address(target) -> tuple[str, int | None]:
    """Host and port out of a connect argument, or ``("", None)``.

    AF_INET is a 2-tuple, AF_INET6 a 4-tuple, AF_UNIX a path. Only the first two
    carry a host and a port.
    """
    if not isinstance(target, (tuple, list)) or len(target) < 2:
        return "", None
    host = str(target[0] or "")
    try:
        port = int(target[1])
    except (TypeError, ValueError):
        port = None
    return host, port


def _refuse(host: str, port: int | None) -> None:
    """Raise for a tastytrade host or a TWS port on this machine. Nothing else."""
    lowered = str(host or "").lower()
    if any(domain in lowered for domain in TASTY_DOMAINS):
        raise BlockedConnection(
            f"test tried to connect to tastytrade at {host}:{port}; use the Sess and "
            "Transport doubles in test_tasty.py"
        )
    if lowered in LOCALHOSTS and port in TWS_PORTS:
        raise BlockedConnection(
            f"test tried to connect to TWS or IB Gateway at {host}:{port}; use the "
            "_FakeIB object in test_safety.py"
        )


@pytest.fixture(autouse=True)
def isolate_machine(monkeypatch, tmp_path):
    """Hide this machine's broker setup from one test, then put it back.

    ``monkeypatch`` undoes every variable and every patch when the test ends, so
    nothing leaks into the next test or out of the run. Function scope, because
    the isolated home comes from ``tmp_path``.

    Returns the isolated paths so a test can assert on them.
    """
    for name in list(os.environ):
        if name.startswith(PREFIXES):
            monkeypatch.delenv(name, raising=False)
    for name in (*WORKER_NAMES, *OTHER_NAMES):
        monkeypatch.delenv(name, raising=False)

    home = tmp_path / "home"
    cred_dir = home / "no-secrets"
    spx_home = tmp_path / "spx-noon-home"
    for folder in (home, cred_dir, spx_home):
        folder.mkdir(parents=True, exist_ok=True)
    # Path.home() and os.path.expanduser("~") both follow HOME on this Python.
    # USERPROFILE is pinned to the same directory so no interpreter or library
    # that prefers it can reach the real home.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # An empty directory, so tasty.cred_dir() finds no .prod_secret or .sb_secret
    monkeypatch.setenv("TASTYTRADE_CRED_DIR", str(cred_dir))
    monkeypatch.setenv("SPX_NOON_HOME", str(spx_home))
    # A path that does not exist. load_config() only falls back to the repo
    # config.json when SPX_NOON_CONFIG is unset, so this closes that fallback.
    config_path = tmp_path / "no-config.json"
    monkeypatch.setenv("SPX_NOON_CONFIG", str(config_path))

    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, target):
        _refuse(*_address(target))
        return real_connect(self, target)

    def create_connection(target, *args, **kwargs):
        _refuse(*_address(target))
        return real_create_connection(target, *args, **kwargs)

    def getaddrinfo(host, port, *args, **kwargs):
        # The only place a tastytrade hostname is still a hostname when something
        # dials it through asyncio or httpx: by the time connect() sees it, it is
        # an IP. Localhost ports are left to connect(), which sees them plainly.
        _refuse(str(host or ""), None)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    return SimpleNamespace(
        home=home, cred_dir=cred_dir, spx_home=spx_home, config_path=config_path,
        original_home=Path(ORIGINAL_HOME) if ORIGINAL_HOME else None,
    )
