"""Console alerts, logs/alerts.log, a Windows toast when one is available, and the yes-prompt."""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class Notifier:
    def __init__(self, logs_dir: Path, toast: bool = True):
        self.path = Path(logs_dir) / "alerts.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.toast_on = toast and sys.platform == "win32"

    def alert(self, title: str, body: str, toast: bool = True) -> None:
        stamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
        block = f"\n===== {stamp} | {title} =====\n{body}\n"
        print(block, flush=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(block)
        if toast and self.toast_on:
            self._toast(title, body.splitlines()[0] if body else "")

    def _toast(self, title: str, line: str) -> None:
        try:
            from win11toast import notify
            notify(title, line)
            return
        except Exception:  # noqa: BLE001
            pass
        safe_title = title.replace("'", "''")[:60]
        safe_line = line.replace("'", "''")[:200]
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]>$null;"
            "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            f"$n=$x.GetElementsByTagName('text');$n.Item(0).AppendChild($x.CreateTextNode('{safe_title}'))>$null;"
            f"$n.Item(1).AppendChild($x.CreateTextNode('{safe_line}'))>$null;"
            "$m=[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Noon breakout');"
            "$m.Show([Windows.UI.Notifications.ToastNotification]::new($x))"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass


class ConsoleApprover:
    """Wait for ``yes <ticket id>`` in this console. Anything else, or the timeout, skips."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdin
        self._q: queue.Queue | None = None

    def _start(self) -> None:
        if self._q is not None:
            return
        self._q = queue.Queue()

        def reader():
            for line in self.stream:
                self._q.put(line.strip())

        threading.Thread(target=reader, daemon=True).start()

    def ask(self, ticket_id: str, timeout: float, clock=None) -> tuple[bool, str]:
        import time as _t
        clock = clock or _t.monotonic
        self._start()
        while not self._q.empty():
            self._q.get_nowait()
        print(
            f">>> Type   yes {ticket_id}   within {timeout:.0f}s to SEND this order "
            "(anything else = skip): ",
            end="", flush=True)
        end = clock() + timeout
        while True:
            left = end - clock()
            if left <= 0:
                return False, "timeout"
            try:
                line = self._q.get(timeout=left)
            except queue.Empty:
                return False, "timeout"
            parts = line.lower().split()
            if parts == ["yes", ticket_id.lower()]:
                return True, "approved"
            if parts and parts[0] in ("no", "n", "skip"):
                return False, "declined"
            print(f"(need exactly: yes {ticket_id})", flush=True)
