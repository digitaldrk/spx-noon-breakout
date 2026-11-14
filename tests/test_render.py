"""The hosted tastytrade worker: the env pin, the service clock, and the blueprint.

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

from spx_noon import resume, service
from spx_noon.config import ENV_FIELDS as config_env_fields
from spx_noon.config import Config, SafetyError, load_config, resolve_broker

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

PIN = "SPX_NOON_BROKER"
ENV_FIELDS = ("SPX_NOON_MODE", "SPX_NOON_TASTY_DATA", "SPX_NOON_TASTY_ORDERS", "SPX_NOON_ACCOUNT")
#: Set by the blueprint, pasted in the dashboard, or read by tasty.load_creds.
SECRET_NAMES = (
    "SPX_NOON_ACCOUNT",
    "TASTYTRADE_PROD_CLIENT_SECRET",
    "TASTYTRADE_PROD_REFRESH_TOKEN",
    "TASTYTRADE_SANDBOX_CLIENT_SECRET",
    "TASTYTRADE_SANDBOX_REFRESH_TOKEN",
)
PLAIN_NAMES = (
    "SPX_NOON_HOME",
    "PYTHONUNBUFFERED",
    "SPX_NOON_BROKER",
    "SPX_NOON_MODE",
    "SPX_NOON_TASTY_DATA",
    "SPX_NOON_TASTY_ORDERS",
)

MONDAY = date(2026, 11, 16)

#: flock, SIGKILL, and a child that can ignore SIGTERM. The worker runs on Linux;
#: on Windows spx_noon is the launcher in run_session.ps1, not this.
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX flock and signals")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with the worker's variables unset."""
    for name in (PIN, "SPX_NOON_HOME", "SPX_NOON_CONFIG", *ENV_FIELDS):
        monkeypatch.delenv(name, raising=False)


def _write(tmp_path, name, data):
    path = Path(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_text(json.dumps(data))
    return target


# --------------------------------------------------------------------------
# The broker pin
# --------------------------------------------------------------------------

def test_linux_auto_is_ibkr_until_the_env_pins_tasty(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    path = _write(tmp_path, "config.json", {"broker": "tasty", "mode": "dry"})
    assert resolve_broker("auto") == "ibkr"
    assert load_config(path).broker == "ibkr"          # a file broker key stays ignored
    monkeypatch.setenv(PIN, "tasty")
    for asked in ("auto", "", None):
        assert resolve_broker(asked) == "tasty"
    assert load_config(path).broker == "tasty"
    assert load_config(path, broker="auto").broker == "tasty"
    # an explicit broker argument never consults the variable
    assert resolve_broker("ibkr") == "ibkr"
    assert load_config(path, broker="ibkr").broker == "ibkr"
    monkeypatch.setenv(PIN, "ibkr")
    assert resolve_broker("auto") == "ibkr"
    assert resolve_broker("tastytrade") == "tasty"
    monkeypatch.setenv(PIN, "  TT  ")
    assert resolve_broker("auto") == "tasty"
    monkeypatch.setenv(PIN, "")
    assert resolve_broker("auto") == "ibkr"
    monkeypatch.setenv(PIN, "nonsense")
    with pytest.raises(SafetyError, match=PIN):
        resolve_broker("auto")


def test_the_pin_leaves_the_mac_and_windows_machines_alone(monkeypatch, tmp_path):
    path = _write(tmp_path, "config.json", {"broker": "tasty", "mode": "dry"})
    for platform, machine in (("darwin", "ibkr"), ("win32", "tasty")):
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.delenv(PIN, raising=False)
        assert resolve_broker("auto") == machine
        assert load_config(path).broker == machine


# --------------------------------------------------------------------------
# The four env fields
# --------------------------------------------------------------------------

def _linux_tasty(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv(PIN, "tasty")


def test_the_env_fields_do_nothing_until_they_are_set(monkeypatch, tmp_path):
    _linux_tasty(monkeypatch)
    path = _write(tmp_path, "config.json", {
        "mode": "dry", "tasty_data": "production", "tasty_orders": "sandbox", "account": "111"})
    for unset in ((), ("", "", "", "")):
        for name, value in zip(ENV_FIELDS, unset):
            monkeypatch.setenv(name, value)
        cfg = load_config(path)
        assert (cfg.mode, cfg.tasty_data, cfg.tasty_orders, cfg.account) == \
            ("dry", "production", "sandbox", "111")
        assert cfg.dry_run is True and cfg.sends_orders() is False
    monkeypatch.setenv("SPX_NOON_MODE", "live")
    monkeypatch.setenv("SPX_NOON_ACCOUNT", "705")
    cfg = load_config(path)
    assert cfg.mode == "live" and cfg.account == "705"
    assert cfg.dry_run is False and cfg.live is True and cfg.sends_orders() is True
    assert cfg.real_money is False               # sandbox orders are not real money
    # an explicit argument still wins over the variable
    assert load_config(path, mode="dry").mode == "dry"
    assert load_config(path, account="999").account == "999"


def test_nothing_else_is_an_env_knob(monkeypatch, tmp_path):
    """The list is closed: no port, no host, no retired live switch, no order toggle."""
    _linux_tasty(monkeypatch)
    path = _write(tmp_path, "config.json", {"mode": "live", "account": "705"})
    for name, value in (
        ("TASTYTRADE_ORDERS", "production"),
        ("tasty_allow_production_orders", "1"),
        ("SPX_NOON_ALLOW_LIVE", "1"),
        ("SPX_NOON_LIVE_ACCOUNT", "U7654321"),
        ("SPX_NOON_PORT", "7496"),
        ("SPX_NOON_HOST", "1.2.3.4"),
        ("SPX_NOON_TOAST", "1"),
    ):
        monkeypatch.setenv(name, value)
    cfg = load_config(path)
    assert cfg.tasty_orders == "sandbox" and cfg.real_money is False
    assert cfg.port == 7497 and cfg.host == "127.0.0.1"
    assert sorted(config_env_fields) == sorted(ENV_FIELDS)


def test_a_file_base_dir_still_beats_spx_noon_home(monkeypatch, tmp_path):
    _linux_tasty(monkeypatch)
    monkeypatch.setenv("SPX_NOON_HOME", str(tmp_path / "disk"))
    pinned = _write(tmp_path, "pinned.json", {"mode": "dry", "base_dir": str(tmp_path / "chosen")})
    assert load_config(pinned).home == tmp_path / "chosen"
    blank = _write(tmp_path, "blank.json", {"mode": "dry"})
    assert load_config(blank).home == tmp_path / "disk"


def test_production_orders_still_need_production_data(monkeypatch, tmp_path):
    _linux_tasty(monkeypatch)
    path = _write(tmp_path, "config.json", {"mode": "live", "account": "705"})
    monkeypatch.setenv("SPX_NOON_TASTY_ORDERS", "production")
    monkeypatch.setenv("SPX_NOON_TASTY_DATA", "sandbox")
    with pytest.raises(SafetyError, match="tasty_orders=production"):
        load_config(path)
    monkeypatch.setenv("SPX_NOON_TASTY_DATA", "production")
    cfg = load_config(path)
    assert cfg.real_money is True and cfg.live is True and cfg.sends_orders() is True
    monkeypatch.setenv("SPX_NOON_TASTY_ORDERS", "sandbox")
    assert load_config(path).real_money is False


# --------------------------------------------------------------------------
# The seed and the config path
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


def test_the_seed_loads_as_a_sandbox_worker(monkeypatch, tmp_path):
    _linux_tasty(monkeypatch)
    monkeypatch.setenv("SPX_NOON_HOME", str(tmp_path))
    cfg = load_config(ROOT / "config.render.example.json")
    assert cfg.broker == "tasty" and cfg.mode == "live" and cfg.dry_run is False
    assert cfg.real_money is False and cfg.live is True
    assert cfg.home == tmp_path                   # the seed has no base_dir of its own
    assert cfg.t("start_not_before") == time(11, 45)


def test_a_blank_disk_is_seeded_once_and_never_overwritten(tmp_path):
    target = tmp_path / "data" / "config.json"
    assert service.seed_config(target) is True
    assert json.loads(target.read_text())["tasty_orders"] == "sandbox"
    target.write_text(json.dumps({"mode": "dry", "account": "mine"}))
    assert service.seed_config(target) is False
    assert json.loads(target.read_text()) == {"mode": "dry", "account": "mine"}


def test_the_worker_reads_the_config_off_the_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("SPX_NOON_HOME", str(tmp_path))
    assert service.config_path() == tmp_path / "config.json"
    monkeypatch.setenv("SPX_NOON_CONFIG", str(tmp_path / "other.json"))
    assert service.config_path() == tmp_path / "other.json"


def test_the_worker_refuses_a_broker_that_is_not_tastytrade(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("SPX_NOON_HOME", str(tmp_path))
    monkeypatch.setenv("SPX_NOON_MODE", "dry")       # a config IBKR would otherwise accept
    assert service.main() == 2
    out = capsys.readouterr().out
    assert "refusing to start" in out and "tastytrade only" in out
    assert (tmp_path / "config.json").exists()       # it seeded the disk first


def test_the_worker_refuses_production_orders_on_sandbox_data(monkeypatch, tmp_path, capsys):
    _linux_tasty(monkeypatch)
    monkeypatch.setenv("SPX_NOON_HOME", str(tmp_path))
    monkeypatch.setenv("SPX_NOON_TASTY_ORDERS", "production")
    monkeypatch.setenv("SPX_NOON_TASTY_DATA", "sandbox")
    assert service.main() == 2
    out = capsys.readouterr().out
    assert "refusing to start" in out and "tasty_orders=production" in out


def test_the_wrap_child_writes_the_paper_book_and_sends_nothing(monkeypatch, tmp_path):
    """The real 16:20 command, run in process: no socket, no order, paper book."""
    from spx_noon.wrap_run import main as wrap_main

    _linux_tasty(monkeypatch)
    monkeypatch.setenv("SPX_NOON_HOME", str(tmp_path))
    monkeypatch.setenv("SPX_NOON_MODE", "live")
    config = tmp_path / "config.json"
    assert service.seed_config(config) is True
    assert wrap_main(["--config", str(config), "--now"]) == 0
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
    assert launcher.argvs() == [["-m", "spx_noon", "wrap", "--now"]]
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
    assert seen["argv"] == [sys.executable, "-m", "spx_noon"]
    assert seen["env"]["SPX_NOON_CONFIG"] == str(marker)
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
    assert env["SPX_NOON_HOME"]["value"] == "/data"
    assert env["PYTHONUNBUFFERED"]["value"] == "1"
    assert env["SPX_NOON_BROKER"]["value"] == "tasty"
    assert env["SPX_NOON_MODE"]["value"] == "live"
    assert env["SPX_NOON_TASTY_DATA"]["value"] == "production"
    assert env["SPX_NOON_TASTY_ORDERS"]["value"] == "sandbox"
    for name in SECRET_NAMES:
        assert env[name] == {"key": name, "sync": False}
    for name in PLAIN_NAMES:
        assert "sync" not in env[name]
    text = (ROOT / "render.yaml").read_text().lower()
    for leak in ("secret:", "token:", "password"):
        assert leak not in text


def test_the_image_has_no_port_no_health_check_and_copies_no_state():
    text = (ROOT / "Dockerfile").read_text()
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert lines[0] == "FROM python:3.11-slim"
    assert not any(line.upper().startswith(("EXPOSE", "HEALTHCHECK")) for line in lines)
    assert 'CMD ["python", "-m", "spx_noon.service"]' in text
    assert "tzdata" in text and "'.[tasty]'" in text
    assert "SPX_NOON_HOME=/data" in text and "PYTHONUNBUFFERED=1" in text
    copied = [line.split()[1] for line in lines if line.startswith("COPY ")]
    assert copied == ["pyproject.toml", "spx_noon", service.SEED_NAME]
    ignored = (ROOT / ".dockerignore").read_text()
    for name in ("config.json", "secrets/", "tokens.json", ".env", "state/", "logs/", "reports/"):
        assert name in ignored
    assert f"!{service.SEED_NAME}" in ignored


def test_render_md_covers_the_four_operator_jobs():
    text = (ROOT / "RENDER.md").read_text()
    for needle in ("Blueprint", "Manual Deploy", "Logs", "/data", "Another account",
                   "SPX_NOON_BROKER=tasty", "REAL MONEY=NO", "11:45", "16:20", "15:10"):
        assert needle in text, needle
    for name in SECRET_NAMES:
        assert name in text
    for name in PLAIN_NAMES:
        assert name in text
    assert "python -m spx_noon wrap --now" in text
    assert "RENDER.md" in (ROOT / "README.md").read_text()


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
