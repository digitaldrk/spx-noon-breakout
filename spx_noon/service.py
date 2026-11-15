"""Always-on worker: one supervisor, one session child per trading day.

``python -m spx_noon.service`` is the container entrypoint. It never trades
itself. It seeds a blank disk with the committed example config, waits for the
next session, spawns ``python -m spx_noon`` at 11:45 ET, restarts that child a
bounded number of times if it dies badly, and runs the read-only
``python -m spx_noon wrap --now`` once at 16:20 ET. Weekends, NYSE holidays,
early closes, and years with no holiday list are skipped.

Plain variable names, no bot prefix, the same set every bot uses on Render. The
Mac and Windows launchers keep ``SPX_NOON_HOME`` and ``SPX_NOON_CONFIG``; this
process reads neither. ``DATA`` is the mounted disk, not the Unix ``HOME``.

  ``DATA``          disk mount, default ``/data``  -> ``base_dir`` for the children
  ``BROKER``        required: tasty (tt, tastytrade); anything else is refused
  ``MODE``          live (default) | dry | alerts
  ``TASTY_DATA``    production (default) | sandbox
  ``TASTY_ORDERS``  sandbox (default) | production  <- real money, with production data
  ``TASTY_ACCOUNT`` blank means the first account on the order session

Those six win over ``$DATA/config.json``, and the resolved, secret-free result is
published to ``$DATA/state/service-config.json``, which is the file both children
load. The seed is never overwritten and the environment is never logged. A worker
environment that still carries one of the retired ``SPX_NOON_*`` names without
the new one refuses to start, naming the new variable: an operator who renamed
nothing must not get the default instead of what they set.

``BROKER`` has to be set, and it has to resolve to tastytrade. The children are
launched with that broker on the command line. Nothing is inferred from Linux.

Log lines are plain and start with one of ``boot``, ``waiting``, ``running``,
``done``, ``skip``, ``restart``, or ``error``.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:  # POSIX only; the hosted worker is Linux
    import fcntl
except ImportError:  # pragma: no cover - Windows keeps the pid-file gate
    fcntl = None

from . import resume
from .calendar import fallback_status
from .config import Config, SafetyError, load_config

ET = ZoneInfo("America/New_York")
log = logging.getLogger("spx_noon.service")

#: Committed, secret-free config copied onto a blank disk.
SEED_NAME = "config.render.example.json"
#: One session child at a time, under the home directory on the disk.
LOCK_NAME = "service.lock"
#: The resolved, secret-free config the children load, under ``state`` on the disk.
#: Never the seed: ``$DATA/config.json`` is the operator's file and is not rewritten.
SERVICE_CONFIG = "service-config.json"

#: The mounted disk when ``DATA`` is unset or blank.
DEFAULT_DATA = "/data"
#: This worker is tastytrade, whatever the machine would pick on its own.
BROKER = "tasty"
#: Spellings of that one broker. ``ibkr`` is not one of them.
TASTY_BROKERS = ("tasty", "tt", "tastytrade")
#: Modes a tastytrade session accepts. ``live`` is the only one that transmits.
MODES = ("live", "dry", "alerts")

#: A retired worker name and the name that replaced it. An environment that still
#: has the old one and not the new one fails closed: the old value is never read,
#: never logged, and never falls through to a default.
RETIRED_NAMES = {
    "SPX_NOON_HOME": "DATA",
    "SPX_NOON_BROKER": "BROKER",
    "SPX_NOON_MODE": "MODE",
    "SPX_NOON_TASTY_DATA": "TASTY_DATA",
    "SPX_NOON_TASTY_ORDERS": "TASTY_ORDERS",
    "SPX_NOON_ACCOUNT": "TASTY_ACCOUNT",
}

#: The read-only wrap. 16:20 ET matches the Mac launchd job.
WRAP_AT = time(16, 20)
#: The runner itself stops looking for an entry at 15:10 ET with nothing open.
RESTART_UNTIL = time(15, 10)
#: At most three restarts, so a crash loop cannot run all day.
RESTART_BACKOFFS = (5.0, 15.0, 45.0)
#: Longest uninterruptible nap, so SIGTERM is answered promptly.
TICK = 5.0
#: How often a running child is looked at.
CHILD_POLL = 5.0
#: After terminate, how long a child gets before it is killed. Render allows
#: about 30 seconds between its SIGTERM and SIGKILL; this stays well inside it.
CHILD_GRACE_S = 10.0
#: A child that starts after the wrap hour still gets this long to finish.
LATE_CHILD_GRACE = timedelta(minutes=40)

#: The children are told the broker on the command line. A file cannot name one.
SESSION_ARGV = ("-m", "spx_noon", "--broker", BROKER)
WRAP_ARGV = ("-m", "spx_noon", "wrap", "--now", "--broker", BROKER)


def seed_source() -> Path | None:
    """The committed seed, next to the package or in the working directory."""
    for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
        candidate = base / SEED_NAME
        if candidate.is_file():
            return candidate
    return None


def env_data(env=None) -> str:
    """``DATA``, the mounted disk. Blank or unset is ``/data``. Not the Unix ``HOME``."""
    env = os.environ if env is None else env
    return (env.get("DATA") or "").strip() or DEFAULT_DATA


def config_path(env=None) -> Path:
    """``$DATA/config.json``, the operator's file on the disk.

    ``SPX_NOON_CONFIG`` and ``SPX_NOON_HOME`` are the local launchers' variables
    and take no part here, so a stale one cannot point the worker at another file.
    """
    return Path(os.path.expanduser(env_data(env))) / "config.json"


def check_retired(env=None) -> None:
    """Refuse to start when a renamed variable is still the only one that is set.

    Applying the old value would be guessing, and defaulting would quietly turn
    ``SPX_NOON_TASTY_ORDERS=production`` back into sandbox orders or
    ``SPX_NOON_MODE=dry`` into a mode that transmits. The error names the new
    variable and nothing else: an old value can be an account id, and RENDER.md
    is where the pairs are written down.
    """
    env = os.environ if env is None else env
    stale = [(old, new) for old, new in RETIRED_NAMES.items()
             if old in env and not str(env.get(new, "") or "").strip()]
    if not stale:
        return
    raise SafetyError(
        "a retired variable is set and its replacement is not. Set "
        + ", ".join(new for _old, new in stale)
        + " in the service environment, then delete the retired key. RENDER.md lists the "
          "pairs. The retired value is not read and not printed."
    )


def env_overrides(env=None) -> dict:
    """The six worker variables as ``load_config`` overrides. These win over the file.

    Plain names, no bot prefix. A missing or blank value is the default in the
    table at the top of this module; a value that is not accepted is a
    :class:`SafetyError`, not a silent fallback.
    """
    env = os.environ if env is None else env
    mode = (env.get("MODE") or "").strip().lower() or "live"
    if mode not in MODES:
        raise SafetyError(f"MODE must be live, dry, or alerts (got {mode!r})")
    broker = (env.get("BROKER") or "").strip().lower()
    if not broker:
        raise SafetyError("BROKER must be set to tasty, tt, or tastytrade")
    if broker not in TASTY_BROKERS:
        raise SafetyError(
            f"BROKER must be tasty, tt, or tastytrade on this worker (got {broker!r})")
    over = {"broker": BROKER, "base_dir": env_data(env), "mode": mode}
    for name, field in (("TASTY_DATA", "tasty_data"), ("TASTY_ORDERS", "tasty_orders")):
        value = (env.get(name) or "").strip().lower()
        if value:                        # blank leaves the file's value, then the default
            over[field] = value
    if "TASTY_ACCOUNT" in env:           # present and blank means the first account
        over["account"] = (env["TASTY_ACCOUNT"] or "").strip()
    return over


def build_config(path: str | os.PathLike | None = None, env=None) -> Config:
    """The config both children run with: the disk file, then the six variables.

    ``load_config`` validates it, so production orders on sandbox data never get
    this far.
    """
    env = os.environ if env is None else env
    cfg = load_config(path, **env_overrides(env))
    if cfg.broker != BROKER:             # belt and braces; env_overrides pins it
        raise SafetyError(f"this worker is tastytrade only (got {cfg.broker!r})")
    return cfg


def real_money(cfg: Config) -> bool:
    """Production data, production orders, live mode, and not a dry run. All four.

    ``Config.real_money`` follows the order host alone, which is what the session
    itself cares about. This is the worker's boot line: a dry run whose orders are
    production transmits nothing, so it is not real money.
    """
    return (cfg.broker == BROKER
            and str(cfg.tasty_data).strip().lower() == "production"
            and str(cfg.tasty_orders).strip().lower() == "production"
            and cfg.mode == "live" and not cfg.dry_run)


def publish_config(cfg: Config, path: str | os.PathLike) -> Path:
    """Write the resolved, secret-free config the children load.

    Not the seed: ``$DATA/config.json`` stays the operator's file. ``broker`` is
    left out because a file cannot pick the broker — the children get
    ``--broker tasty`` on the command line. No credential is a config field.
    """
    data = {key: (list(value) if isinstance(value, tuple) else value)
            for key, value in cfg.to_dict().items() if key != "broker"}
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def seed_config(path: str | os.PathLike) -> bool:
    """Copy the seed when the disk has no config. An existing file is never touched."""
    path = Path(path)
    if path.exists():
        return False
    source = seed_source()
    if source is None:
        raise SafetyError(f"no {SEED_NAME} to seed {path} from")
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, path)
    return True


def _lock_holder(path: Path) -> int:
    try:
        return int((path.read_text() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


class Lock:
    """One worker at a time, so two session children cannot run.

    ``flock`` is the gate: the kernel hands it to exactly one open file
    description and drops it when that process dies, so a lock left behind by a
    killed worker is not stale and needs no pid guessing. The pid is written
    into the file for a human reading it over a shell, and the file is never
    unlinked: deleting it would let the next worker lock a different inode while
    somebody still holds this one.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._handle = None

    def take(self, pid: int | None = None) -> bool:
        pid = int(os.getpid() if pid is None else pid)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            return self._take_without_flock(pid)
        handle = open(self.path, "a+")  # noqa: SIM115 - held for the life of the worker
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._stamp(handle, pid)
        self._handle = handle
        return True

    @staticmethod
    def _stamp(handle, text) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{text}\n" if text else "")
        handle.flush()

    def _take_without_flock(self, pid: int) -> bool:
        """No flock, so creating the file is the only atomic step there is.

        A file that is already there means a worker, not a guess about whether
        its pid is alive: guessing is what the race was. One left behind by a
        crash is removed by hand. The hosted worker is Linux and never gets here.
        """
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(handle, "w") as fh:
            fh.write(f"{pid}\n")
        self._handle = pid
        return True

    def fds(self) -> tuple:
        """The descriptor a session child inherits, so the lock outlives a
        supervisor that is killed while its child is still trading."""
        handle = self._handle
        if handle is None or isinstance(handle, int):
            return ()
        return (handle.fileno(),)

    def release(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        if isinstance(handle, int):          # the no-flock path owns the name itself
            try:
                self.path.unlink()
            except OSError:
                pass
            return
        try:
            self._stamp(handle, "")          # nobody holds it now; best effort
        except OSError:
            pass
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


class Clock:
    """Wall clock in Eastern time. Tests pass their own."""

    def now(self) -> datetime:
        return datetime.now(ET)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            _time.sleep(seconds)


def _spell(span: timedelta) -> str:
    total = max(0, int(span.total_seconds()))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


class Supervisor:
    """The day loop. Nothing here talks to a broker."""

    def __init__(self, cfg, *, clock=None, launch=None, config_file=None, keep_fds=()):
        self.cfg = cfg
        self.clock = clock or Clock()
        self._launch = launch or self._spawn
        self.config_file = Path(config_file) if config_file else None
        self.keep_fds = tuple(keep_fds)
        self.stopping = False
        self._child: subprocess.Popen | None = None

    # ---- plumbing ----------------------------------------------------

    @property
    def start_at(self) -> time:
        return self.cfg.t("start_not_before")

    def say(self, event: str, message: str = "") -> str:
        line = f"{event} {message}".strip()
        log.info("%s", line)
        return line

    def _spawn(self, argv, deadline: datetime | None = None) -> int:
        """Run one child and wait. A crash is its exit code, not an exception.

        ``deadline`` is a backstop, not a trading rule. The runner loops forever
        on a spread it has given up selling (three failed sells, then an alert),
        and a child that never exits would cost every later trading day, not
        just today's. The deadline is the wrap hour: an hour and twenty minutes
        after the last possible flat and after the options close, so stopping
        the child there cannot shorten a session or change an exit. Nothing
        stops it inside trading hours.
        """
        env = dict(os.environ)
        if self.config_file is not None:
            # The resolved config, which the child already honors this variable for.
            # It is how the six worker variables reach the process that trades: the
            # child reads no environment field of its own.
            env["SPX_NOON_CONFIG"] = str(self.config_file)
        # The child inherits the lock descriptor: if this supervisor is killed
        # while the session is still trading, the lock stays held and a
        # replacement worker refuses to start a second session on top of it.
        child = subprocess.Popen([sys.executable, *argv], env=env, pass_fds=self.keep_fds)
        self._child = child
        try:
            return self._watch_child(child, " ".join(argv), deadline)
        finally:
            self._child = None

    def _watch_child(self, child, name: str, deadline: datetime | None) -> int:
        """Wait for the child. Once it has been asked to stop, the wait is bounded."""
        kill_after = None
        while True:
            try:
                return int(child.wait(timeout=CHILD_POLL))
            except subprocess.TimeoutExpired:
                pass
            if kill_after is not None:
                if _time.monotonic() >= kill_after:
                    self.say("error", f"{name} ignored the stop; killing it")
                    child.kill()
                    return int(child.wait())
                continue
            if self.stopping:
                self._stop_child()
                kill_after = _time.monotonic() + CHILD_GRACE_S
            elif deadline is not None and self.clock.now() >= deadline:
                if self.position_open(self.clock.now().date()):
                    self.say("error", f"{name} still has a position at {deadline:%H:%M} ET, "
                                      "after the close. Check state/own_position.json by hand.")
                self.say("error", f"{name} is still running at {deadline:%H:%M} ET; stopping it")
                self._stop_child()
                kill_after = _time.monotonic() + CHILD_GRACE_S

    def launch(self, argv, deadline: datetime | None = None) -> int:
        """A child that will not start is a crash, not a dead supervisor."""
        try:
            return int(self._launch(list(argv), deadline))
        except OSError as exc:  # fork failed, interpreter missing
            self.say("error", f"could not start {' '.join(argv)}: {exc}")
            return 127

    def _stop_child(self) -> None:
        child = self._child
        if child is None:
            return
        try:
            if child.poll() is None:
                child.terminate()
        except OSError:  # already gone
            pass

    def install_signals(self) -> None:
        for name in ("SIGTERM", "SIGINT"):
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                signal.signal(number, self.on_signal)
            except (ValueError, OSError):  # not the main thread
                pass

    def on_signal(self, signum, _frame=None) -> None:
        """Stop starting work, ask the child to stop, and let ``run`` unwind."""
        if not self.stopping:
            self.stopping = True
            self.say("done", f"signal {int(signum)}; stopping")
        self._stop_child()

    def sleep(self, seconds: float) -> None:
        remaining = float(seconds)
        while remaining > 0 and not self.stopping:
            step = min(TICK, remaining)
            self.clock.sleep(step)
            remaining -= step

    def sleep_until(self, target: datetime, what: str) -> None:
        now = self.clock.now()
        if target <= now:
            return
        self.say("waiting", f"{what} at {target:%Y-%m-%d %H:%M} ET, {_spell(target - now)} away")
        while not self.stopping:
            delay = (target - self.clock.now()).total_seconds()
            if delay <= 0:
                return
            self.clock.sleep(min(TICK, delay))

    # ---- the day -----------------------------------------------------

    def position_open(self, day: date) -> bool:
        """Today's own spread is still open or working, so a restart can finish it."""
        try:
            return resume.load_open(self.cfg.state_dir, day) is not None
        except OSError:
            return False

    def in_session_window(self, when: datetime, day: date) -> bool:
        """Before 15:10 ET today, or any time today's position is still live."""
        if when.date() == day and when.time() < RESTART_UNTIL:
            return True
        return self.position_open(day)

    def child_deadline(self, day: date) -> datetime:
        """The wrap hour, or 40 minutes out for a child that starts after it."""
        return max(datetime.combine(day, WRAP_AT, ET), self.clock.now() + LATE_CHILD_GRACE)

    def run_session(self, day: date) -> None:
        """Run the child, restarting a crash at most three times."""
        attempts = 0
        code = 0
        while not self.stopping:
            # Checked here, after any backoff, so a retry that lands past 15:10
            # with nothing open does not go out.
            if attempts and not self.in_session_window(self.clock.now(), day):
                self.say("error", f"session {day} exited {code}; past {RESTART_UNTIL:%H:%M} ET "
                                  "with nothing open, so no restart")
                return
            self.say("running", f"session {day} try {attempts + 1}")
            code = self.launch(SESSION_ARGV, self.child_deadline(day))
            if code == 0:
                self.say("done", f"session {day} finished")
                return
            if self.stopping:
                return
            if attempts >= len(RESTART_BACKOFFS):
                self.say("error", f"session {day} exited {code}; {attempts + 1} tries is enough for today")
                return
            delay = RESTART_BACKOFFS[attempts]
            attempts += 1
            self.say("restart", f"session {day} exited {code}; try {attempts + 1} in {delay:.0f}s")
            self.sleep(delay)

    def wrap_path(self, day: date) -> Path:
        return self.cfg.reports_dir / f"wrap-{day.isoformat()}.md"

    def run_wrap(self, day: date) -> None:
        """Read-only. One attempt a day; a failure is logged, not retried."""
        if self.wrap_path(day).exists():
            self.say("skip", f"wrap {day} is already written")
            return
        self.say("running", f"wrap {day}")
        code = self.launch(WRAP_ARGV)
        if code == 0:
            self.say("done", f"wrap {day} written")
        else:
            self.say("error", f"wrap {day} exited {code}; not retried today")

    def day(self, day: date) -> None:
        start = datetime.combine(day, self.start_at, ET)
        if self.clock.now() < start:
            self.sleep_until(start, f"the {day} session")
        if self.stopping:
            return
        if self.in_session_window(self.clock.now(), day):
            self.run_session(day)
        else:
            self.say("skip", f"{day} session window is over and nothing is open")
        if self.stopping:
            return
        wrap_at = datetime.combine(day, WRAP_AT, ET)
        if self.clock.now() < wrap_at:
            self.sleep_until(wrap_at, f"the {day} wrap")
        if self.stopping:
            return
        self.run_wrap(day)

    def next_day_start(self, when: datetime) -> datetime:
        return datetime.combine(when.date() + timedelta(days=1), time(0, 0, 30), ET)

    def run(self, max_days: int | None = None) -> int:
        """Loop until SIGTERM. ``max_days`` is for tests."""
        days = 0
        while not self.stopping and (max_days is None or days < max_days):
            days += 1
            now = self.clock.now()
            day = now.date()
            status, reason = fallback_status(day)
            try:
                if status == "OPEN":
                    self.day(day)
                else:
                    self.say("skip", f"{day} is {status}: {reason}")
            except Exception:  # noqa: BLE001 - one bad day must not take the worker down
                log.exception("error %s did not finish; waiting for the next day", day)
            if self.stopping:
                break
            self.sleep_until(self.next_day_start(self.clock.now()), "the next day")
        return 0


def _setup_logging(home: Path) -> None:
    folder = Path(home) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.FileHandler(folder / "service.log"), logging.StreamHandler(sys.stdout)],
        )


def main(argv=None) -> int:
    try:
        check_retired()
        path = config_path()
        seeded = seed_config(path)
        cfg = build_config(path)
        cfg.ensure_dirs()
    except (SafetyError, ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        print(f"refusing to start: {exc}", flush=True)
        return 2
    _setup_logging(cfg.home)
    # The lock comes before anything is written: the published config is the file
    # the running worker's next child loads, so a worker that is turned away here
    # must not have changed that worker's mode, order environment, or account.
    lock = Lock(cfg.home / LOCK_NAME)
    if not lock.take():
        log.error("error another spx-noon worker holds %s; stopping", lock.path)
        return 2
    try:
        published = publish_config(cfg, cfg.state_dir / SERVICE_CONFIG)
    except OSError as exc:
        log.error("error could not write %s: %s; stopping", SERVICE_CONFIG, exc)
        lock.release()
        return 2
    from .tasty import host_for, safety_banner

    if seeded:
        log.info("boot seeded %s from %s", path, SEED_NAME)
    log.info(
        "boot broker=%s mode=%s home=%s config=%s child-config=%s %s",
        cfg.broker, cfg.mode, cfg.home, path, published,
        safety_banner(host_for(cfg.tasty_data), host_for(cfg.tasty_orders),
                      cfg.tasty_orders, real=real_money(cfg)),
    )
    worker = Supervisor(cfg, config_file=published, keep_fds=lock.fds())
    worker.install_signals()
    try:
        return worker.run()
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
