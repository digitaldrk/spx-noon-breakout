"""The hosted tastytrade worker: its plain env names, the service clock, and the blueprint.

Nothing here reaches tastytrade, IBKR, or Render. The session child is a stub and
the clock is fake, so a whole trading day runs in milliseconds.
"""
import json
import os
import signal
import subprocess
import sys
from time import sleep as _real_sleep
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from spx_noon import config as config_module
from spx_noon import resume, service
from spx_noon.config import Config, SafetyError, load_config, resolve_broker

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

#: The worker's own settings. Plain names, no bot prefix, the same set on every bot.
WORKER_ENV = ("DATA", "BROKER", "MODE", "TASTY_DATA", "TASTY_ORDERS", "TASTY_ACCOUNT")
#: Renamed. One of these without its replacement is a refusal, not a default.
RETIRED_ENV = (
    "SPX_NOON_HOME",
    "SPX_NOON_BROKER",
    "SPX_NOON_MODE",
    "SPX_NOON_TASTY_DATA",
    "SPX_NOON_TASTY_ORDERS",
    "SPX_NOON_ACCOUNT",
)
#: Pasted in the dashboard: the account to trade, and what tasty.load_creds reads.
SECRET_NAMES = (
    "TASTY_ACCOUNT",
    "TASTYTRADE_PROD_CLIENT_SECRET",
    "TASTYTRADE_PROD_REFRESH_TOKEN",
    "TASTYTRADE_SANDBOX_CLIENT_SECRET",
    "TASTYTRADE_SANDBOX_REFRESH_TOKEN",
)
PLAIN_NAMES = ("DATA", "PYTHONUNBUFFERED", "BROKER", "MODE", "TASTY_DATA", "TASTY_ORDERS")

MONDAY = date(2026, 11, 16)

#: flock, SIGKILL, and a child that can ignore SIGTERM. The worker runs on Linux;
#: on Windows spx_noon is the launcher in run_session.ps1, not this.
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX flock and signals")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with the worker's and the launchers' variables unset."""
    for name in (*WORKER_ENV, *RETIRED_ENV, "SPX_NOON_CONFIG"):
        monkeypatch.delenv(name, raising=False)


def _write(tmp_path, name, data):
    path = Path(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_text(json.dumps(data))
    return target


def _disk(tmp_path, **data):
    """A mounted disk with the operator's config.json on it."""
    _write(tmp_path, "config.json", data)
    return Path(tmp_path)


def _env(base, **extra):
    """The worker's environment: the disk and an explicit broker, plus the dashboard."""
    return {"DATA": str(base), "BROKER": "tasty", **extra}


# --------------------------------------------------------------------------
# The broker: the machine's, or the one the worker asks for by name
# --------------------------------------------------------------------------

def test_a_missing_broker_is_refused_on_every_platform(monkeypatch, tmp_path):
    """Nothing infers IBKR or tastytrade from the operating system or the environment."""
    path = _write(tmp_path, "config.json", {"broker": "tasty", "mode": "dry"})
    for platform in ("linux", "darwin", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        for name in ("BROKER", "SPX_NOON_BROKER"):
            monkeypatch.setenv(name, "tasty")
        for asked in ("auto", "", None):
            with pytest.raises(SafetyError, match="broker must be set"):
                resolve_broker(asked)
        with pytest.raises(SafetyError, match="broker must be set"):
            load_config(path)
    for asked in ("tasty", "tt", "tastytrade", "  TASTY  "):
        assert resolve_broker(asked) == "tasty"
    assert load_config(path, broker="tt").broker == "tasty"
    assert load_config(path, broker="ibkr").broker == "ibkr"   # a file key cannot override it
    with pytest.raises(SafetyError, match="unknown broker"):
        resolve_broker("nonsense")


# --------------------------------------------------------------------------
# The worker's six variables
# --------------------------------------------------------------------------

def test_the_defaults_are_the_disk_live_mode_and_sandbox_orders(tmp_path):
    base = _disk(tmp_path, toast=False)
    cfg = service.build_config(base / "config.json", _env(base))
    assert cfg.broker == "tasty" and cfg.mode == "live" and cfg.dry_run is False
    assert cfg.tasty_data == "production" and cfg.tasty_orders == "sandbox"
    assert cfg.account == "" and cfg.home == base
    assert service.real_money(cfg) is False
    assert cfg.live is True and cfg.sends_orders() is True
    # DATA is the disk, and /data when it is unset or blank
    assert service.env_data({}) == service.DEFAULT_DATA == "/data"
    assert service.env_data({"DATA": "   "}) == "/data"
    with pytest.raises(SafetyError, match="BROKER must be set"):
        service.env_overrides({})
    assert service.env_overrides({"BROKER": "tasty"})["base_dir"] == "/data"
    assert service.config_path({}) == Path("/data/config.json")
    assert service.config_path({"DATA": "/mnt/two"}) == Path("/mnt/two/config.json")


def test_every_accepted_value_is_taken_and_nothing_else_is(tmp_path):
    base = _disk(tmp_path, mode="dry", account="file-account")

    def cfg(**env):
        return service.build_config(base / "config.json", _env(base, **env))

    for broker in ("tasty", "tt", "tastytrade", "  TASTY  "):
        assert cfg(BROKER=broker).broker == "tasty"
    for blank in ("", "   "):
        with pytest.raises(SafetyError, match="BROKER must be set"):
            cfg(BROKER=blank)
    for mode in ("live", "dry", "alerts"):
        got = cfg(MODE=mode)
        assert got.mode == mode
        assert got.dry_run is (mode != "live") and got.sends_orders() is (mode == "live")
    for blank in ("", "   "):
        assert cfg(MODE=blank).mode == "live"          # the code default, as on Fly
    for host in ("production", "sandbox"):
        assert cfg(TASTY_DATA=host).tasty_data == host
        assert cfg(TASTY_DATA="production", TASTY_ORDERS=host).tasty_orders == host
    assert cfg(TASTY_DATA="  PRODUCTION  ").tasty_data == "production"

    for bad in ("paper", "live-ish", "LIVE!"):
        with pytest.raises(SafetyError, match="MODE"):
            cfg(MODE=bad)
    for bad in ("ibkr", "sim", "schwab"):
        with pytest.raises(SafetyError, match="BROKER"):
            cfg(BROKER=bad)
    for bad in ("prod", "cert", "yes"):
        with pytest.raises(SafetyError, match="tasty_data"):
            cfg(TASTY_DATA=bad)
        with pytest.raises(SafetyError, match="tasty_orders"):
            cfg(TASTY_ORDERS=bad)


def test_tasty_account_is_the_key_being_there_not_its_value(tmp_path):
    base = _disk(tmp_path, mode="dry", account="file-account")

    def cfg(**env):
        return service.build_config(base / "config.json", _env(base, **env))

    assert cfg().account == "file-account"             # the key is absent: the file stands
    assert cfg(TASTY_ACCOUNT="5WX12345").account == "5WX12345"
    assert cfg(TASTY_ACCOUNT="  5WX12345  ").account == "5WX12345"
    assert cfg(TASTY_ACCOUNT="").account == ""         # present and blank: the first account
    assert "account" not in service.env_overrides(_env(base))


def test_real_money_needs_production_data_production_orders_and_live_mode(tmp_path):
    base = _disk(tmp_path, mode="dry")

    def cfg(**env):
        return service.build_config(base / "config.json", _env(base, **env))

    assert service.real_money(cfg()) is False                      # sandbox orders
    assert service.real_money(cfg(TASTY_ORDERS="production")) is True
    assert service.real_money(cfg(TASTY_DATA="production")) is False
    for quiet in ("dry", "alerts"):
        hushed = cfg(TASTY_ORDERS="production", MODE=quiet)
        assert hushed.tasty_orders == "production"     # the host is what it says
        assert service.real_money(hushed) is False     # but nothing transmits
        assert hushed.sends_orders() is False
    # the production-data lock is untouched
    with pytest.raises(SafetyError, match="tasty_orders=production"):
        cfg(TASTY_DATA="sandbox", TASTY_ORDERS="production")
    assert service.real_money(cfg(TASTY_DATA="sandbox")) is False


def test_an_old_name_on_its_own_refuses_to_start_and_names_the_new_one(tmp_path):
    """Never the old value, never a default. This is the renamed-dashboard case."""
    assert service.RETIRED_NAMES == {
        "SPX_NOON_HOME": "DATA",
        "SPX_NOON_BROKER": "BROKER",
        "SPX_NOON_MODE": "MODE",
        "SPX_NOON_TASTY_DATA": "TASTY_DATA",
        "SPX_NOON_TASTY_ORDERS": "TASTY_ORDERS",
        "SPX_NOON_ACCOUNT": "TASTY_ACCOUNT",
    }
    for old, new in service.RETIRED_NAMES.items():
        for value in ("5WX12345", "production", "dry", ""):
            for fresh in ({}, {new: ""}, {new: "   "}):      # absent, blank, whitespace
                with pytest.raises(SafetyError) as caught:
                    service.check_retired({old: value, **fresh})
                message = str(caught.value)
                assert new in message                        # the variable to set
                assert old not in message and "SPX_NOON" not in message
                if value:
                    assert value not in message              # never the retired value


def test_the_new_name_wins_and_the_old_one_is_never_read(tmp_path):
    base = _disk(tmp_path, mode="dry")
    env = _env(base, BROKER="tasty", MODE="live", TASTY_DATA="production",
               TASTY_ORDERS="sandbox", TASTY_ACCOUNT="mine",
               SPX_NOON_HOME="/nowhere", SPX_NOON_BROKER="ibkr", SPX_NOON_MODE="dry",
               SPX_NOON_TASTY_DATA="sandbox", SPX_NOON_TASTY_ORDERS="production",
               SPX_NOON_ACCOUNT="theirs")
    service.check_retired(env)                     # every new name is set; nothing is refused
    cfg = service.build_config(base / "config.json", env)
    assert cfg.broker == "tasty" and cfg.mode == "live" and cfg.home == base
    assert cfg.tasty_data == "production" and cfg.tasty_orders == "sandbox"
    assert cfg.account == "mine" and service.real_money(cfg) is False


def test_the_workers_list_of_variables_is_closed(tmp_path):
    """No port, no host, no order toggle, and the unread names stay unread."""
    base = _disk(tmp_path, mode="dry")
    noise = {
        "PORT": "7496",
        "HOST": "1.2.3.4",
        "ACCOUNT": "U7654321",
        "TASTYTRADE_ORDERS": "production",
        "DRY_RUN": "0",
        "TOAST": "1",
        "SPX_NOON_CONFIG": str(tmp_path / "nowhere.json"),
        "SPX_NOON_HOST": "1.2.3.4",
        "SPX_NOON_PORT": "7496",
        "SPX_NOON_ALLOW_LIVE": "1",
        "SPX_NOON_LIVE_ACCOUNT": "U7654321",
    }
    env = {**noise, **_env(base)}
    service.check_retired(env)                     # none of those names is guarded
    cfg = service.build_config(base / "config.json", env)
    assert cfg.tasty_orders == "sandbox" and service.real_money(cfg) is False
    assert cfg.port == 7497 and cfg.host == "127.0.0.1"
    assert cfg.account == "" and cfg.home == base
    assert sorted(service.env_overrides(_env(base))) == ["base_dir", "broker", "mode"]


def test_load_config_reads_no_config_field_from_the_environment(monkeypatch, tmp_path):
    """The four SPX_NOON_ fields are gone from the loader, and the plain names
    are the worker's own: the Mac process has no environment knobs either way."""
    assert not hasattr(config_module, "ENV_FIELDS")
    assert not hasattr(config_module, "env_overrides")
    monkeypatch.setattr(sys, "platform", "darwin")
    path = _write(tmp_path, "config.json", {"mode": "paper", "port": 7497, "account": "D111"})
    for name, value in (
        ("SPX_NOON_MODE", "live"),
        ("SPX_NOON_TASTY_DATA", "sandbox"),
        ("SPX_NOON_TASTY_ORDERS", "production"),
        ("SPX_NOON_ACCOUNT", "U7654321"),
        ("MODE", "live"),
        ("TASTY_DATA", "sandbox"),
        ("TASTY_ORDERS", "production"),
        ("TASTY_ACCOUNT", "U7654321"),
        ("BROKER", "tasty"),
    ):
        monkeypatch.setenv(name, value)
    cfg = load_config(path, broker="ibkr")
    with pytest.raises(SafetyError, match="broker must be set"):
        load_config(path)
    assert cfg.broker == "ibkr" and cfg.mode == "paper" and cfg.account == "D111"
    assert cfg.tasty_data == "production" and cfg.tasty_orders == "sandbox"
    assert cfg.paper is True and cfg.real_money is False and cfg.real_money_flag == "NO"


def test_the_mac_launchers_keep_spx_noon_home_and_spx_noon_config(monkeypatch, tmp_path):
    """Requirement 3: the local split. A file base_dir still beats SPX_NOON_HOME."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SPX_NOON_HOME", str(tmp_path / "disk"))
    pinned = _write(tmp_path, "pinned.json", {"mode": "dry", "base_dir": str(tmp_path / "chosen")})
    assert load_config(pinned, broker="ibkr").home == tmp_path / "chosen"
    blank = _write(tmp_path, "blank.json", {"mode": "dry"})
    assert load_config(blank, broker="ibkr").home == tmp_path / "disk"
    monkeypatch.setenv("SPX_NOON_CONFIG", str(pinned))
    assert load_config(broker="ibkr").home == tmp_path / "chosen"
    # and the worker reads neither of them
    assert service.env_overrides({"SPX_NOON_HOME": str(tmp_path / "disk"),
                                  "DATA": str(tmp_path / "mount"),
                                  "BROKER": "tasty"})["base_dir"] \
        == str(tmp_path / "mount")


# --------------------------------------------------------------------------
# The seed, the config path, and the file the children load
# --------------------------------------------------------------------------

def test_the_seed_carries_no_secrets_no_base_dir_and_no_broker():
    text = (ROOT / "config.render.example.json").read_text()
    data = json.loads(text)
    assert data["mode"] == "live"
    assert data["tasty_data"] == "production" and data["tasty_orders"] == "sandbox"
    for key in ("base_dir", "broker", "dry_run"):
        assert key not in data
    lowered = text.lower()
    for word in ("client_secret", "refresh", "private_key", "password", "bearer"):
        assert word not in lowered
    for name in SECRET_NAMES:
        assert name not in text
    for name in RETIRED_ENV:                       # the comment names no retired variable
        assert name not in text


def test_the_seed_loads_as_a_sandbox_worker(tmp_path):
    cfg = service.build_config(ROOT / "config.render.example.json", _env(tmp_path))
    assert cfg.broker == "tasty" and cfg.mode == "live" and cfg.dry_run is False
    assert service.real_money(cfg) is False and cfg.live is True
    assert cfg.home == tmp_path                   # the seed has no base_dir; DATA is it
    assert cfg.t("start_not_before") == time(11, 45)


def test_a_blank_disk_is_seeded_once_and_never_overwritten(tmp_path):
    target = tmp_path / "data" / "config.json"
    assert service.seed_config(target) is True
    assert json.loads(target.read_text())["tasty_orders"] == "sandbox"
    target.write_text(json.dumps({"mode": "dry", "account": "mine"}))
    assert service.seed_config(target) is False
    assert json.loads(target.read_text()) == {"mode": "dry", "account": "mine"}


def test_the_worker_reads_the_config_off_the_disk_and_ignores_the_local_variables(
        monkeypatch, tmp_path):
    monkeypatch.setenv("DATA", str(tmp_path))
    assert service.config_path() == tmp_path / "config.json"
    monkeypatch.setenv("SPX_NOON_CONFIG", str(tmp_path / "other.json"))
    assert service.config_path() == tmp_path / "config.json"
    monkeypatch.delenv("DATA")
    assert service.config_path() == Path("/data/config.json")


def _recording(taken):
    """A Supervisor that records how main() wired it up, then runs no day at all."""
    real = service.Supervisor

    class Recorded(real):
        def __init__(self, cfg, **kwargs):
            taken["cfg"] = cfg
            taken["config_file"] = Path(kwargs["config_file"])
            taken["keep_fds"] = kwargs.get("keep_fds", ())
            super().__init__(cfg, **kwargs)

        def run(self, max_days=None):
            return 0

    return Recorded


def test_the_published_config_is_what_the_children_load(monkeypatch, tmp_path):
    """Requirement: a production order environment has to reach the process that trades."""
    monkeypatch.setenv("DATA", str(tmp_path))
    monkeypatch.setenv("BROKER", "tasty")
    monkeypatch.setenv("TASTY_ORDERS", "production")
    monkeypatch.setenv("TASTY_ACCOUNT", "5WX12345")
    taken = {}
    monkeypatch.setattr(service, "Supervisor", _recording(taken))
    assert service.main() == 0

    seed = json.loads((tmp_path / "config.json").read_text())
    assert seed["tasty_orders"] == "sandbox"          # the operator's file is left alone
    published = tmp_path / "state" / service.SERVICE_CONFIG
    assert taken["config_file"] == published
    child = json.loads(published.read_text())
    assert child["tasty_orders"] == "production" and child["tasty_data"] == "production"
    assert child["mode"] == "live" and child["account"] == "5WX12345"
    assert child["base_dir"] == str(tmp_path)         # state, logs, reports stay on the disk
    assert "broker" not in child                      # a file cannot pick the broker
    for word in ("client_secret", "refresh_token", "password"):
        assert word not in published.read_text().lower()
    # the child loads that file and gets the environment the operator set
    reloaded = load_config(published, broker="tasty")
    assert reloaded.tasty_orders == "production" and reloaded.real_money is True
    assert reloaded.home == tmp_path


@posix_only
def test_a_refused_second_worker_cannot_rewrite_the_published_config(
        monkeypatch, tmp_path, caplog):
    """The lock is taken before anything is published.

    The published file is what the running worker's *next* child loads, so a
    second worker that is turned away must not leave its own mode, order
    environment, or account behind on the disk.
    """
    monkeypatch.setenv("DATA", str(tmp_path))
    monkeypatch.setenv("BROKER", "tasty")
    monkeypatch.setattr(service, "Supervisor", _recording({}))
    assert service.main() == 0                        # the first worker publishes
    published = tmp_path / "state" / service.SERVICE_CONFIG
    before = published.read_text()
    assert json.loads(before)["mode"] == "live"
    assert json.loads(before)["tasty_orders"] == "sandbox"

    held = service.Lock(tmp_path / service.LOCK_NAME)
    assert held.take() is True                        # somebody else is running now
    try:
        monkeypatch.setenv("MODE", "dry")
        monkeypatch.setenv("TASTY_ORDERS", "production")
        monkeypatch.setenv("TASTY_ACCOUNT", "theirs")
        caplog.clear()
        with caplog.at_level("ERROR", logger="spx_noon.service"):
            assert service.main() == 2
        assert any("holds" in line for line in caplog.messages), caplog.messages
    finally:
        held.release()
    assert published.read_text() == before            # the running worker is untouched


def test_the_boot_line_names_the_hosts_and_the_real_money_rule_and_no_secret(
        monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(service, "Supervisor", _recording({}))
    monkeypatch.setenv("DATA", str(tmp_path))
    monkeypatch.setenv("BROKER", "tasty")
    monkeypatch.setenv("TASTY_ACCOUNT", "5WX12345")

    def boot():
        caplog.clear()
        with caplog.at_level("INFO", logger="spx_noon.service"):
            assert service.main() == 0
        return next(line for line in caplog.messages if line.startswith("boot broker"))

    line = boot()
    assert "broker=tasty mode=live" in line and f"home={tmp_path}" in line
    assert "DATA=api.tastyworks.com ORDERS=api.cert.tastyworks.com REAL MONEY=NO" in line
    assert "5WX12345" not in line                     # the account is not in a boot line
    for name in (*RETIRED_ENV, "TASTY_ACCOUNT", "TASTY_ORDERS", "TASTY_DATA", "BROKER=",
                 "TASTYTRADE_PROD_REFRESH_TOKEN"):
        assert name not in line                       # no environment dump, no secret

    monkeypatch.setenv("TASTY_ORDERS", "production")
    assert "ORDERS=api.tastyworks.com REAL MONEY=YES" in boot()
    for quiet in ("dry", "alerts"):                   # production orders, but nothing sends
        monkeypatch.setenv("MODE", quiet)
        assert "ORDERS=api.tastyworks.com REAL MONEY=NO" in boot()


def test_the_worker_refuses_a_broker_that_is_not_tastytrade(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATA", str(tmp_path))
    monkeypatch.setenv("BROKER", "ibkr")
    assert service.main() == 2
    out = capsys.readouterr().out
    assert "refusing to start" in out and "BROKER must be tasty" in out
    assert (tmp_path / "config.json").exists()       # it seeded the disk first


def test_the_worker_refuses_production_orders_on_sandbox_data(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATA", str(tmp_path))
    monkeypatch.setenv("BROKER", "tasty")
    monkeypatch.setenv("TASTY_ORDERS", "production")
    monkeypatch.setenv("TASTY_DATA", "sandbox")
    assert service.main() == 2
    out = capsys.readouterr().out
    assert "refusing to start" in out and "tasty_orders=production" in out
    assert not (tmp_path / "state" / service.SERVICE_CONFIG).exists()


def test_the_worker_refuses_to_boot_on_a_retired_name_alone(monkeypatch, tmp_path, capsys):
    """An existing service that still has only SPX_NOON_TASTY_ORDERS fails closed."""
    monkeypatch.setenv("DATA", str(tmp_path))
    monkeypatch.setenv("SPX_NOON_TASTY_ORDERS", "production")
    monkeypatch.setenv("SPX_NOON_MODE", "dry")
    assert service.main() == 2
    out = capsys.readouterr().out
    assert "refusing to start" in out
    assert "TASTY_ORDERS" in out and "MODE" in out
    assert "SPX_NOON_" not in out and "production" not in out and "dry" not in out
    assert not (tmp_path / "config.json").exists()   # it refused before touching the disk


def test_the_wrap_child_writes_the_paper_book_and_sends_nothing(monkeypatch, tmp_path):
    """The real 16:20 command, run in process: no socket, no order, paper book."""
    from spx_noon.wrap_run import main as wrap_main

    monkeypatch.setattr(sys, "platform", "linux")
    config = tmp_path / "config.json"
    assert service.seed_config(config) is True
    cfg = service.build_config(config, _env(tmp_path))
    published = service.publish_config(cfg, cfg.state_dir / service.SERVICE_CONFIG)
    # exactly the argv the supervisor uses, minus the "wrap" the CLI has already eaten
    assert wrap_main([*service.WRAP_ARGV[3:], "--config", str(published)]) == 0
    today = datetime.now(ET).date()
    assert (tmp_path / "reports" / f"wrap-{today.isoformat()}.md").exists()
    assert not (tmp_path / "reports" / f"wrap-live-{today.isoformat()}.md").exists()


# --------------------------------------------------------------------------
# The lock
# --------------------------------------------------------------------------

@posix_only
def test_the_lock_keeps_a_second_worker_out(tmp_path):
    path = tmp_path / service.LOCK_NAME
    first = service.Lock(path)
    assert first.take() is True
    assert service._lock_holder(path) == os.getpid()
    second = service.Lock(path)
    assert second.take() is False                 # a different open file, same flock
    assert service._lock_holder(path) == os.getpid()
    first.release()
    # the file stays: unlinking it would let the next worker lock a fresh inode
    assert path.exists() and service._lock_holder(path) == 0
    assert second.take() is True
    assert service._lock_holder(path) == os.getpid()
    second.release()


@posix_only
def test_a_lock_a_killed_worker_left_behind_is_not_stale(tmp_path):
    """The kernel drops the flock when the holder dies, racing or not."""
    path = tmp_path / service.LOCK_NAME
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time, fcntl;"
         f"fh = open({str(path)!r}, 'a+');"
         "fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB);"
         "print('held', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert service.Lock(path).take() is False     # the other worker is alive
        holder.kill()
    finally:
        holder.wait()
    taken = service.Lock(path)
    assert taken.take() is True                       # and gone means gone
    taken.release()


@posix_only
def test_a_session_child_keeps_the_lock_when_its_supervisor_is_killed(tmp_path):
    """The lock is about two session children, not two supervisors.

    A killed supervisor is the kernel closing its handle, which is what is done
    here by hand. The child inherited the descriptor, so the lock stays held and
    a replacement worker cannot start a second session on top of a live one.
    """
    path = tmp_path / service.LOCK_NAME
    lock = service.Lock(path)
    assert lock.take() is True
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True, pass_fds=lock.fds())
    try:
        assert child.stdout.readline().strip() == "up"
        lock._handle.close()                 # the supervisor dies; the child lives on
        lock._handle = None
        assert service.Lock(path).take() is False
    finally:
        child.kill()
        child.wait()
    taken = service.Lock(path)
    for _ in range(100):                     # and it comes free once that child is gone
        if taken.take():
            break
        _real_sleep(0.02)
    else:
        pytest.fail("the lock never came free after the session child died")
    taken.release()


def test_without_flock_an_existing_lock_file_is_refused(tmp_path, monkeypatch):
    """Windows has no flock, so creating the file is the only atomic step."""
    monkeypatch.setattr(service, "fcntl", None)
    path = tmp_path / service.LOCK_NAME
    mine = service.Lock(path)
    assert mine.take(pid=os.getpid()) is True
    assert service._lock_holder(path) == os.getpid()
    assert service.Lock(path).take(pid=os.getpid()) is False   # no pid guessing
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path.write_text(f"{dead.pid}\n")
    assert service.Lock(path).take(pid=os.getpid()) is False   # a dead pid is still a refusal
    mine.release()
    assert not path.exists()
    assert service.Lock(path).take(pid=os.getpid()) is True


# --------------------------------------------------------------------------
# The service clock
# --------------------------------------------------------------------------

def _cfg(tmp_path, **extra):
    data = dict(broker="tasty", mode="live", account="705", base_dir=str(tmp_path), toast=False)
    data.update(extra)
    cfg = Config(**data)
    cfg.validate()
    cfg.ensure_dirs()
    return cfg


class FakeClock:
    """Time only moves when something sleeps."""

    def __init__(self, start):
        self.moment = start
        self.naps = 0

    def now(self):
        return self.moment

    def sleep(self, seconds):
        self.naps += 1
        self.moment += timedelta(seconds=max(0.0, float(seconds)))


class Launcher:
    """Stands in for the child process: records the call, burns clock, returns a code."""

    def __init__(self, clock, codes=(), runs_for=0.0, cfg=None):
        self.clock = clock
        self.codes = list(codes)
        self.runs_for = float(runs_for)
        self.cfg = cfg
        self.calls = []

    def __call__(self, argv, deadline=None):
        argv = list(argv)
        started = self.clock.now()
        code = self.codes.pop(0) if self.codes else 0
        self.clock.sleep(self.runs_for if argv == list(service.SESSION_ARGV) else 0.0)
        if "wrap" in argv and code == 0 and self.cfg is not None:
            day = started.date()
            (self.cfg.reports_dir / f"wrap-{day.isoformat()}.md").write_text("# wrap\n")
        self.calls.append({"argv": argv, "started": started, "ended": self.clock.now(),
                           "code": code, "deadline": deadline})
        return code

    def argvs(self):
        return [call["argv"] for call in self.calls]


def _watch(supervisor):
    """Collect the plain log lines the supervisor emits."""
    said = []
    spoken = supervisor.say

    def say(event, message=""):
        line = spoken(event, message)
        said.append(line)
        return line

    supervisor.say = say
    return said


def _supervisor(tmp_path, when, *, codes=(), runs_for=0.0, cfg=None):
    cfg = cfg or _cfg(tmp_path)
    clock = FakeClock(when)
    launcher = Launcher(clock, codes=codes, runs_for=runs_for, cfg=cfg)
    supervisor = service.Supervisor(cfg, clock=clock, launch=launcher)
    return supervisor, clock, launcher, cfg


def _at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), ET)


def _save_open(cfg, day, status="open"):
    resume.save(cfg.state_dir, {"date": day.isoformat(), "status": status, "had_fill": True})


def test_a_day_the_market_is_closed_never_starts_a_session(tmp_path):
    days = (
        (date(2026, 11, 14), "weekend"),
        (date(2026, 11, 26), "NYSE holiday"),
        (date(2026, 11, 27), "early close"),
        (date(2028, 6, 5), "no hard-coded holiday list"),
    )
    for day, why in days:
        supervisor, clock, launcher, _cfg_ = _supervisor(tmp_path / day.isoformat(), _at(day, 8))
        said = _watch(supervisor)
        assert supervisor.run(max_days=1) == 0
        assert launcher.calls == []
        assert any(line.startswith("skip") and why in line for line in said), said
        assert clock.now().date() == day + timedelta(days=1)   # it waited for the next day


def test_a_trading_day_starts_at_1145_runs_past_1400_and_wraps_once(tmp_path):
    cfg = _cfg(tmp_path)
    supervisor, clock, launcher, _ = _supervisor(
        tmp_path, _at(MONDAY, 8), codes=[0], runs_for=3 * 3600 + 15 * 60, cfg=cfg)
    said = _watch(supervisor)
    assert supervisor.run(max_days=1) == 0
    assert launcher.argvs() == [list(service.SESSION_ARGV), list(service.WRAP_ARGV)]
    session, wrap = launcher.calls
    assert session["started"] == _at(MONDAY, 11, 45)
    assert session["ended"] == _at(MONDAY, 15, 0)        # nothing stops the child at 14:00
    assert wrap["started"] == _at(MONDAY, 16, 20)
    assert supervisor.wrap_path(MONDAY).exists()
    assert any(line.startswith("waiting") for line in said)
    assert any(line.startswith("running") for line in said)
    assert clock.now().date() == MONDAY + timedelta(days=1)


def test_a_clean_session_exit_is_not_restarted(tmp_path):
    supervisor, _clock, launcher, _ = _supervisor(tmp_path, _at(MONDAY, 11, 45), codes=[0])
    said = _watch(supervisor)
    supervisor.run_session(MONDAY)
    assert len(launcher.calls) == 1
    assert said[-1].startswith("done")


def test_a_crash_restarts_three_times_on_a_backoff_then_stops_for_the_day(tmp_path):
    supervisor, _clock, launcher, _ = _supervisor(tmp_path, _at(MONDAY, 12), codes=[1, -9, 2, 1])
    said = _watch(supervisor)
    supervisor.run_session(MONDAY)
    assert len(launcher.calls) == 4                      # one try plus three restarts
    starts = [call["started"] for call in launcher.calls]
    gaps = [(late - early).total_seconds() for early, late in zip(starts, starts[1:])]
    assert gaps == list(service.RESTART_BACKOFFS) == [5.0, 15.0, 45.0]
    assert sum(line.startswith("restart") for line in said) == 3
    assert said[-1].startswith("error")


def test_a_restart_leaves_todays_position_for_the_child_to_pick_up(tmp_path):
    cfg = _cfg(tmp_path)
    _save_open(cfg, MONDAY)
    supervisor, _clock, launcher, _ = _supervisor(
        tmp_path, _at(MONDAY, 13), codes=[1, 0], cfg=cfg)
    supervisor.run_session(MONDAY)
    assert len(launcher.calls) == 2
    assert supervisor.position_open(MONDAY) is True
    assert resume.load_open(cfg.state_dir, MONDAY)["status"] == "open"


def test_after_1510_a_crash_comes_back_only_while_something_is_open(tmp_path):
    supervisor, _clock, launcher, _ = _supervisor(tmp_path / "flat", _at(MONDAY, 15, 20), codes=[1])
    said = _watch(supervisor)
    supervisor.run_session(MONDAY)
    assert len(launcher.calls) == 1
    assert said[-1].startswith("error") and "nothing open" in said[-1]

    cfg = _cfg(tmp_path / "open")
    for status in ("open", "working"):
        _save_open(cfg, MONDAY, status=status)
        supervisor, _clock, launcher, _ = _supervisor(
            tmp_path / "open", _at(MONDAY, 15, 20), codes=[1, 0], cfg=cfg)
        supervisor.run_session(MONDAY)
        assert len(launcher.calls) == 2
    # yesterday's leftover is not today's position
    _save_open(cfg, MONDAY - timedelta(days=3))
    supervisor, _clock, launcher, _ = _supervisor(
        tmp_path / "open", _at(MONDAY, 15, 20), codes=[1, 0], cfg=cfg)
    supervisor.run_session(MONDAY)
    assert len(launcher.calls) == 1


def test_a_backoff_that_crosses_1510_does_not_launch_on_the_far_side(tmp_path):
    """The window is checked after the wait, not before it."""
    supervisor, clock, launcher, _ = _supervisor(
        tmp_path, _at(MONDAY, 15, 9) + timedelta(seconds=59), codes=[1, 0])
    said = _watch(supervisor)
    supervisor.run_session(MONDAY)
    assert len(launcher.calls) == 1                   # the 5s retry would land at 15:10:04
    assert clock.now().time() > time(15, 10)
    assert said[-1].startswith("error") and "nothing open" in said[-1]


def test_a_late_boot_skips_the_session_unless_a_position_is_open(tmp_path):
    supervisor, _clock, launcher, _ = _supervisor(
        tmp_path / "flat", _at(MONDAY, 17), codes=[0], cfg=_cfg(tmp_path / "flat"))
    said = _watch(supervisor)
    supervisor.day(MONDAY)
    assert launcher.argvs() == [list(service.WRAP_ARGV)]
    assert any(line.startswith("skip") and "window is over" in line for line in said)

    cfg = _cfg(tmp_path / "open")
    _save_open(cfg, MONDAY)
    supervisor, _clock, launcher, _ = _supervisor(
        tmp_path / "open", _at(MONDAY, 17), codes=[0, 0], cfg=cfg)
    supervisor.day(MONDAY)
    assert launcher.argvs() == [list(service.SESSION_ARGV), list(service.WRAP_ARGV)]


def test_the_wrap_is_read_only_runs_once_and_is_not_retried_in_a_loop(tmp_path):
    cfg = _cfg(tmp_path)
    supervisor, _clock, launcher, _ = _supervisor(tmp_path, _at(MONDAY, 16, 20), codes=[0], cfg=cfg)
    supervisor.run_wrap(MONDAY)
    assert launcher.argvs() == [["-m", "spx_noon", "wrap", "--now", "--broker", "tasty"]]
    said = _watch(supervisor)
    supervisor.run_wrap(MONDAY)                       # the report is there now
    assert len(launcher.calls) == 1
    assert said[-1].startswith("skip")

    failing, _clock, launcher, _ = _supervisor(
        tmp_path / "sad", _at(MONDAY, 16, 20), codes=[2, 2])
    said = _watch(failing)
    failing.run_wrap(MONDAY)
    assert len(launcher.calls) == 1                   # one attempt, logged, not retried
    assert said[-1].startswith("error")


def test_sigterm_stops_before_any_new_work_starts(tmp_path):
    supervisor, clock, launcher, _ = _supervisor(tmp_path, _at(MONDAY, 8), codes=[0])
    supervisor.on_signal(signal.SIGTERM)
    assert supervisor.stopping is True
    assert supervisor.run(max_days=1) == 0
    assert launcher.calls == []
    assert clock.now() == _at(MONDAY, 8)              # it did not even sleep


def test_sigterm_in_the_middle_of_a_wait_ends_the_wait(tmp_path):
    supervisor, clock, launcher, _ = _supervisor(tmp_path, _at(MONDAY, 8), codes=[0])
    original = clock.sleep

    def sleep(seconds):
        original(seconds)
        if clock.naps == 4:
            supervisor.on_signal(signal.SIGTERM)

    clock.sleep = sleep
    assert supervisor.run(max_days=1) == 0
    assert launcher.calls == []
    assert clock.now() < _at(MONDAY, 11, 45)


def test_a_child_that_will_not_start_is_a_crash_not_a_dead_worker(tmp_path):
    cfg = _cfg(tmp_path)
    clock = FakeClock(_at(MONDAY, 12))
    tries = []

    def refuse(argv, deadline=None):
        tries.append(list(argv))
        raise OSError("fork: Resource temporarily unavailable")

    supervisor = service.Supervisor(cfg, clock=clock, launch=refuse)
    said = _watch(supervisor)
    supervisor.run_session(MONDAY)
    assert len(tries) == 4                            # the bounded restart still applies
    assert said[-1].startswith("error")


def test_a_day_that_blows_up_waits_for_the_next_one(tmp_path):
    cfg = _cfg(tmp_path)
    clock = FakeClock(_at(MONDAY, 8))
    supervisor = service.Supervisor(cfg, clock=clock, launch=Launcher(clock))

    def explode(_day):
        raise RuntimeError("the disk went away")

    supervisor.day = explode
    assert supervisor.run(max_days=1) == 0            # the worker stays up
    assert clock.now().date() == MONDAY + timedelta(days=1)


class _FakeChild:
    def __init__(self):
        self.alive = True
        self.terminated = 0

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated += 1
        self.alive = False


def test_sigterm_signals_the_running_child(tmp_path):
    supervisor, _clock, _launcher, _ = _supervisor(tmp_path, _at(MONDAY, 12))
    child = _FakeChild()
    supervisor._child = child
    supervisor.on_signal(signal.SIGTERM)
    assert child.terminated == 1
    supervisor.on_signal(signal.SIGTERM)              # a second one is harmless
    assert child.terminated == 1


def test_the_session_child_deadline_is_long_after_the_last_flat(tmp_path):
    cfg = _cfg(tmp_path)
    supervisor, _clock, launcher, _ = _supervisor(tmp_path, _at(MONDAY, 11, 45), codes=[0], cfg=cfg)
    supervisor.run_session(MONDAY)
    # two hours after the last 13:00 decision is 15:00; the backstop is 16:20
    assert launcher.calls[0]["deadline"] == _at(MONDAY, 16, 20)
    late, _clock, launcher, _ = _supervisor(tmp_path, _at(MONDAY, 17), codes=[0], cfg=cfg)
    late.run_session(MONDAY)
    assert launcher.calls[0]["deadline"] == _at(MONDAY, 17) + service.LATE_CHILD_GRACE


@posix_only
def test_a_child_that_never_exits_is_stopped_at_its_deadline(tmp_path, monkeypatch):
    """A real child this time: the runner can sit on a spread it could not sell."""
    monkeypatch.setattr(service, "CHILD_POLL", 0.05)
    cfg = _cfg(tmp_path)
    clock = FakeClock(_at(MONDAY, 16, 20))
    supervisor = service.Supervisor(cfg, clock=clock)
    said = _watch(supervisor)
    code = supervisor._spawn(["-c", "import time; time.sleep(120)"],
                             deadline=_at(MONDAY, 16, 20))
    assert code != 0
    assert supervisor._child is None
    assert any(line.startswith("error") and "still running" in line for line in said)


def test_a_child_inside_trading_hours_is_left_alone(tmp_path, monkeypatch):
    """The 16:20 backstop is not a 14:00 kill: at 14:00 the child runs on."""
    monkeypatch.setattr(service, "CHILD_POLL", 0.05)
    cfg = _cfg(tmp_path)
    _save_open(cfg, MONDAY)
    supervisor = service.Supervisor(cfg, clock=FakeClock(_at(MONDAY, 14)))
    said = _watch(supervisor)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.4)"])
    assert supervisor._watch_child(child, "session", _at(MONDAY, 16, 20)) == 0
    assert said == []                                 # nothing was stopped or logged


def _deaf_child():
    """A child that ignores SIGTERM, and has said so before we signal it."""
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time\n"
         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "print('deaf', flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "deaf"
    return child


@posix_only
def test_a_child_that_ignores_the_stop_is_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "CHILD_POLL", 0.05)
    monkeypatch.setattr(service, "CHILD_GRACE_S", 0.2)
    supervisor = service.Supervisor(_cfg(tmp_path), clock=FakeClock(_at(MONDAY, 16, 20)))
    said = _watch(supervisor)
    deaf = _deaf_child()
    code = supervisor._watch_child(deaf, "session", _at(MONDAY, 16, 20))
    assert code == -signal.SIGKILL
    assert any("killing it" in line for line in said)


@posix_only
def test_sigterm_bounds_the_wait_on_a_deaf_child(tmp_path, monkeypatch):
    """Render kills the container shortly after SIGTERM; the lock must come back."""
    monkeypatch.setattr(service, "CHILD_POLL", 0.05)
    monkeypatch.setattr(service, "CHILD_GRACE_S", 0.2)
    supervisor = service.Supervisor(_cfg(tmp_path), clock=FakeClock(_at(MONDAY, 12)))
    _watch(supervisor)
    deaf = _deaf_child()
    supervisor._child = deaf
    supervisor.on_signal(signal.SIGTERM)
    assert supervisor._watch_child(deaf, "wrap", None) == -signal.SIGKILL


def test_a_child_that_finishes_before_its_deadline_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "CHILD_POLL", 0.05)
    cfg = _cfg(tmp_path)
    supervisor = service.Supervisor(cfg, clock=FakeClock(_at(MONDAY, 12)))
    said = _watch(supervisor)
    assert supervisor._spawn(["-c", "raise SystemExit(0)"], deadline=_at(MONDAY, 16, 20)) == 0
    assert supervisor._spawn(["-c", "raise SystemExit(3)"], deadline=_at(MONDAY, 16, 20)) == 3
    assert said == []


def test_the_child_inherits_the_config_path_and_the_lock(tmp_path, monkeypatch):
    """The disk config, not the one that happens to sit next to the package."""
    cfg = _cfg(tmp_path)
    marker = tmp_path / "config.json"
    supervisor = service.Supervisor(cfg, clock=FakeClock(_at(MONDAY, 12)),
                                    config_file=marker, keep_fds=(7,))
    seen = {}

    class _Popen:
        def __init__(self, argv, env=None, pass_fds=()):
            seen["argv"] = argv
            seen["env"] = env
            seen["pass_fds"] = pass_fds

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(service.subprocess, "Popen", _Popen)
    assert supervisor._spawn(list(service.SESSION_ARGV)) == 0
    assert seen["argv"] == [sys.executable, "-m", "spx_noon", "--broker", "tasty"]
    assert seen["env"]["SPX_NOON_CONFIG"] == str(marker)   # the resolved, published config
    assert seen["pass_fds"] == (7,)          # the lock descriptor goes with it


# --------------------------------------------------------------------------
# The blueprint and the image
# --------------------------------------------------------------------------

def _scalar(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        return raw


def _yaml(text):
    """A reader for the small subset render.yaml uses: nested maps and one list."""
    lines = [line.rstrip() for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    position = 0

    def indent_of(index):
        return len(lines[index]) - len(lines[index].lstrip())

    def block(indent):
        nonlocal position
        if lines[position].lstrip().startswith("- "):
            items = []
            while position < len(lines) and indent_of(position) == indent \
                    and lines[position].lstrip().startswith("- "):
                lines[position] = " " * (indent + 2) + lines[position].lstrip()[2:]
                items.append(block(indent + 2))
            return items
        found = {}
        while position < len(lines):
            here = indent_of(position)
            if here < indent or (here == indent and lines[position].lstrip().startswith("- ")):
                break
            key, _colon, rest = lines[position].strip().partition(":")
            position += 1
            if rest.strip():
                found[key] = _scalar(rest)
            elif position < len(lines) and indent_of(position) > here:
                found[key] = block(indent_of(position))
            else:
                found[key] = None
        return found

    return block(0)


def _worker():
    blueprint = _yaml((ROOT / "render.yaml").read_text())
    assert list(blueprint) == ["services"]
    services = blueprint["services"]
    assert isinstance(services, list) and len(services) == 1
    return services[0]


def test_the_blueprint_is_one_starter_worker_with_a_disk_and_no_port():
    worker = _worker()
    assert worker["type"] == "worker"
    assert worker["runtime"] == "docker"
    assert worker["plan"] == "starter"
    assert worker["region"] == "ohio"
    assert worker["name"] == "spx-noon"
    assert worker["dockerCommand"] == "python -m spx_noon.service"
    assert worker["disk"] == {"name": "spx-noon-data", "mountPath": "/data", "sizeGB": 1}
    assert worker["autoDeploy"] is False
    for absent in ("healthCheckPath", "numInstances", "scaling", "domains", "buildCommand",
                   "startCommand", "previewsEnabled"):
        assert absent not in worker
    text = (ROOT / "render.yaml").read_text()
    for forbidden in ("type: web", "type: pserv", "type: cron", "type: static",
                      "healthCheck", "port:", "numInstances"):
        assert forbidden not in text


def test_the_blueprint_sets_the_plain_env_and_leaves_the_five_secrets_blank():
    worker = _worker()
    env = {item["key"]: item for item in worker["envVars"]}
    assert set(env) == set(PLAIN_NAMES) | set(SECRET_NAMES)
    assert env["DATA"]["value"] == "/data"
    assert env["PYTHONUNBUFFERED"]["value"] == "1"
    assert env["BROKER"]["value"] == "tasty"
    assert env["MODE"]["value"] == "live"
    assert env["TASTY_DATA"]["value"] == "production"
    assert env["TASTY_ORDERS"]["value"] == "sandbox"
    for name in SECRET_NAMES:
        assert env[name] == {"key": name, "sync": False}
    for name in PLAIN_NAMES:
        assert "sync" not in env[name]
    text = (ROOT / "render.yaml").read_text()
    assert "key: SPX_NOON_" not in text      # no retired key is left in the blueprint
    for leak in ("secret:", "token:", "password"):
        assert leak not in text.lower()


def test_the_image_has_no_port_no_health_check_and_copies_no_state():
    text = (ROOT / "Dockerfile").read_text()
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert lines[0] == "FROM python:3.11-slim"
    assert not any(line.upper().startswith(("EXPOSE", "HEALTHCHECK")) for line in lines)
    assert 'CMD ["python", "-m", "spx_noon.service"]' in text
    assert "tzdata" in text and "'.[tasty]'" in text
    assert "DATA=/data" in text and "PYTHONUNBUFFERED=1" in text
    # the image sets no retired name, and no default that would hide a dashboard
    # which still has only the old one
    for absent in (*(f"{name}=" for name in RETIRED_ENV),
                   "MODE=", "TASTY_ORDERS=", "TASTY_DATA=", "BROKER=", "TASTY_ACCOUNT="):
        assert absent not in text, absent
    copied = [line.split()[1] for line in lines if line.startswith("COPY ")]
    assert copied == ["pyproject.toml", "spx_noon", service.SEED_NAME]
    ignored = (ROOT / ".dockerignore").read_text()
    for name in ("config.json", "secrets/", "tokens.json", ".env", "state/", "logs/", "reports/"):
        assert name in ignored
    assert f"!{service.SEED_NAME}" in ignored


def test_render_md_covers_the_four_operator_jobs():
    text = (ROOT / "RENDER.md").read_text()
    for needle in ("Blueprint", "Manual Deploy", "Logs", "/data", "Another account",
                   "BROKER=tasty", "REAL MONEY=NO", "REAL MONEY=YES",
                   "11:45", "16:20", "15:10"):
        assert needle in text, needle
    for name in (*SECRET_NAMES, *PLAIN_NAMES, *RETIRED_ENV):
        assert name in text, name              # the new names, and the pairs to rename
    assert "python -m spx_noon wrap --now" in text
    # a second account is a second worker: its own disk, its own account, no code change
    for needle in ("own disk", "TASTY_ACCOUNT", "not a code change"):
        assert needle in text, needle
    assert "RENDER.md" in (ROOT / "README.md").read_text()


def test_the_docs_use_the_new_names_for_the_worker():
    """README and the seed stop telling an operator to set a retired variable."""
    readme = (ROOT / "README.md").read_text()
    for name in ("DATA", "BROKER", "MODE", "TASTY_DATA", "TASTY_ORDERS", "TASTY_ACCOUNT"):
        assert f"`{name}`" in readme, name
    # the local launchers' two variables are the only SPX_NOON_ names left in README
    assert sorted({word.strip("`,.") for word in readme.split()
                   if word.strip("`,.").startswith("SPX_NOON_")}) == \
        ["SPX_NOON_CONFIG", "SPX_NOON_HOME"]
    seed = (ROOT / "config.render.example.json").read_text()
    assert "$DATA/config.json" in seed and "SPX_NOON" not in seed


def test_the_worker_left_no_trace_in_the_mac_and_windows_launchers():
    """The hosted worker is its own entrypoint; the launchers keep their own job.

    What those scripts do is pinned by the launcher and launchd tests in
    test_safety.py. This one is only about the worker not reaching into them.
    """
    for name in ("run.sh", "run_wrap.sh", "run_session.ps1"):
        text = (ROOT / name).read_text().lower()
        for word in ("render", "docker", "spx_noon.service", "/data"):
            assert word not in text
    assert sorted(path.name for path in (ROOT / "deploy").iterdir()) == [
        "com.derek.spxnoon.plist.template",
        "com.derek.spxnoon.wrap.plist.template",
        "install_launchd.sh",
        "uninstall_launchd.sh",
    ]
    blob = "\n".join(path.read_text() for path in (ROOT / "deploy").glob("*")).lower()
    for word in ("render", "docker", "spx_noon.service"):
        assert word not in blob
