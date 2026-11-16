"""The suite ignores this machine's broker setup.

The fixture that does it is ``isolate_machine`` in ``conftest.py``. These tests
are its proof: a credential that was in the environment before pytest started is
not visible here, ``~`` is not the operator's home, ``load_config()`` does not
read the repo ``config.json``, and a connection to tastytrade or TWS raises
instead of dialling.

No credential value is read or printed. ``conftest`` snapshots the names only.
"""
import os
import socket
from pathlib import Path

import pytest

from conftest import (
    LOCALHOSTS,
    ORIGINAL_HOME,
    PRESENT_AT_IMPORT,
    REPLACED_NAMES,
    TWS_PORTS,
    BlockedConnection,
    _refuse,
)
from spx_noon.config import SafetyError, load_config
from spx_noon.tasty import CERT_HOST, PROD_HOST, cred_dir, load_creds


def test_the_machines_tastytrade_variables_are_not_visible(isolate_machine):
    """A real ``TASTYTRADE_PROD_CLIENT_SECRET`` in the shell does not reach a test."""
    for name in PRESENT_AT_IMPORT:
        if name in REPLACED_NAMES:
            continue
        assert name not in os.environ, name          # the name, never the value
    # the three the fixture replaces are the isolated paths, whatever the shell said
    assert os.environ["TASTYTRADE_CRED_DIR"] == str(isolate_machine.cred_dir)
    assert os.environ["SPX_NOON_HOME"] == str(isolate_machine.spx_home)
    assert os.environ["SPX_NOON_CONFIG"] == str(isolate_machine.config_path)
    for name in (
        "TASTYTRADE_PROD_CLIENT_SECRET", "TASTYTRADE_PROD_REFRESH_TOKEN",
        "TASTYTRADE_SANDBOX_CLIENT_SECRET", "TASTYTRADE_SANDBOX_REFRESH_TOKEN",
    ):
        assert os.environ.get(name) is None
    for name in ("DATA", "BROKER", "MODE", "TASTY_DATA", "TASTY_ORDERS", "TASTY_ACCOUNT",
                 "SPX_SHEETS_CREDENTIALS_JSON"):
        assert name not in os.environ, name


def test_the_credential_directory_is_an_empty_temporary_one(isolate_machine):
    """``cred_dir()`` is the fixture's directory, not ``$HOME/secrets``."""
    folder = cred_dir()
    assert folder == isolate_machine.cred_dir
    assert folder.is_dir()
    assert sorted(path.name for path in folder.iterdir()) == []
    for name in (".prod_secret", ".prod_refresh", ".sb_secret", ".sb_refresh"):
        assert not (folder / name).exists(), name
    if ORIGINAL_HOME:
        assert folder != Path(ORIGINAL_HOME) / "secrets"
    # and with no file and no variable, loading credentials refuses
    for kind in ("production", "sandbox"):
        with pytest.raises(SafetyError, match="missing tastytrade"):
            load_creds(kind)


def test_home_is_the_isolated_one(isolate_machine):
    """``Path.home()`` and ``expanduser`` follow the fixture, not the operator."""
    assert Path.home() == isolate_machine.home
    assert os.path.expanduser("~") == str(isolate_machine.home)
    assert os.environ["USERPROFILE"] == str(isolate_machine.home)
    if ORIGINAL_HOME:
        assert Path.home() != Path(ORIGINAL_HOME)


def test_load_config_does_not_read_the_machines_config_json(isolate_machine):
    """No path means the code defaults plus ``SPX_NOON_HOME``.

    The repo ``config.json`` is gitignored and belongs to whoever runs the suite.
    It is a paper IBKR config with an account and a ``base_dir`` of its own, so a
    dry mode and the isolated home together say it was not read. ``load_config()``
    with no broker raises either way, so that refusal proves nothing.
    """
    assert not isolate_machine.config_path.exists()
    cfg = load_config(broker="ibkr")
    assert cfg.mode == "dry" and cfg.dry_run is True
    assert cfg.home == isolate_machine.spx_home
    assert cfg.home != Path(__file__).resolve().parent.parent
    assert cfg.account == ""                          # no account id from the machine
    assert cfg.port == 7497 and cfg.sends_orders() is False
    # with no file there is no broker to read, and auto is still not a broker
    with pytest.raises(SafetyError) as caught:
        load_config()
    assert str(caught.value) == "broker must be set in config.json: ibkr or tasty"


def test_a_test_cannot_reach_tastytrade(isolate_machine):
    """Both API hosts, by name, through either entry point."""
    for host in (PROD_HOST, CERT_HOST, "api.tastytrade.com", "streamer.tastyworks.com"):
        with pytest.raises(BlockedConnection, match="tastytrade"):
            socket.create_connection((host, 443), timeout=0.01)
        with pytest.raises(BlockedConnection, match="tastytrade"):
            socket.getaddrinfo(host, 443)
        with pytest.raises(BlockedConnection, match="tastytrade"):
            with socket.socket() as sock:
                sock.connect((host, 443))


def test_a_test_cannot_reach_tws_or_ib_gateway(isolate_machine):
    """The four IBKR API ports on this machine, and nothing else."""
    assert sorted(TWS_PORTS) == [4001, 4002, 7496, 7497]
    for host in sorted(LOCALHOSTS):
        for port in sorted(TWS_PORTS):
            with pytest.raises(BlockedConnection, match="TWS or IB Gateway"):
                socket.create_connection((host, port), timeout=0.01)
            with pytest.raises(BlockedConnection, match="TWS or IB Gateway"):
                with socket.socket() as sock:
                    sock.connect((host, port))
    # an IBKR port on another host is not this machine's TWS, and no test dials
    # one: asked directly, the guard lets it by rather than opening a socket here
    for elsewhere in ("10.0.0.5", "192.168.1.10", "127.0.0.2"):
        for port in sorted(TWS_PORTS):
            assert _refuse(elsewhere, port) is None


def test_another_localhost_port_still_connects(isolate_machine):
    """The guard is four ports and two domains. It is not a ban on sockets."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert port not in TWS_PORTS
        with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
            client.sendall(b"hello")
        accepted, _who = listener.accept()
        with accepted:
            assert accepted.recv(5) == b"hello"
    assert socket.getaddrinfo("127.0.0.1", port)[0][4][0] == "127.0.0.1"
