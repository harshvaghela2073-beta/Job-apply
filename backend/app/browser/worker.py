"""Browser worker threads.

Sync Playwright objects must stay on the thread that created them, and a Chrome profile directory
can only be open in one browser at a time. So each browser "owner" gets one thread that runs its
tasks one after another:

- ``linkedin`` and ``naukri``: everything that uses that platform's saved login (login window,
  login check, logged-in scans, deleting the profile).
- ``guest``: public pages with no login (LinkedIn's public job search, Naukri without a login).

A task is a function that receives a fresh ``BrowserDriver``; the driver (and every browser it
opened) is closed when the task returns, so no browser is left running between tasks.
"""

import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import TypeVar

from app.browser.driver import BrowserDriver, BrowserTaskCancelled, BrowserUnavailable
from app.models import Platform

logger = logging.getLogger(__name__)

T = TypeVar("T")
DriverFactory = Callable[[], BrowserDriver]
GUEST = "guest"

# Last browser activity per worker thread (see ``beat``). The watchdog closes a browser whose
# worker has been busy without any activity for ``stall_seconds``: a page call that never returns
# (seen on LinkedIn on 2026-10-14) would otherwise block every later scan.
_heartbeats: dict[int, float] = {}


def beat() -> None:
    """Called by the browser session around every page action."""
    _heartbeats[threading.get_ident()] = time.monotonic()


def _descendant_pids(root: int) -> dict[int, str]:
    """Processes started (directly or not) by ``root``, as pid -> command line."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return {}
    children: dict[int, list[tuple[int, str]]] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        children.setdefault(int(parts[1]), []).append((int(parts[0]), parts[2]))
    found: dict[int, str] = {}
    stack = [root]
    while stack:
        for pid, command in children.get(stack.pop(), []):
            if pid not in found:
                found[pid] = command
                stack.append(pid)
    return found


def kill_browsers(marker: str) -> int:
    """SIGKILL this process's browser processes whose command line contains ``marker``."""
    killed = 0
    for pid, command in _descendant_pids(os.getpid()).items():
        if marker in command and "--type=" not in command:  # the main browser process, not helpers
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    return killed


class BrowserWorker:
    def __init__(self, name: str, driver_factory: DriverFactory):
        self.name = name
        self._driver_factory = driver_factory
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._busy_since: float | None = None
        self._thread_ident: int | None = None
        self.stalled = False

    def idle_seconds(self) -> float | None:
        """Seconds since the running task's last browser activity, or None when idle."""
        started = self._busy_since
        if started is None:
            return None
        last = max(started, _heartbeats.get(self._thread_ident or -1, started))
        return time.monotonic() - last

    def submit(self, task: Callable[[BrowserDriver], T]) -> "Future[T]":
        future: Future[T] = Future()
        with self._lock:
            if self._closed:
                future.set_exception(BrowserTaskCancelled("The browser worker has shut down."))
                return future
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name=f"browser-{self.name}", daemon=True
                )
                self._thread.start()
            self._queue.put((future, task))
        return future

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, task = item
            if not future.set_running_or_notify_cancel():
                continue
            driver = None
            self._thread_ident = threading.get_ident()
            self.stalled = False
            self._busy_since = time.monotonic()
            try:
                driver = self._driver_factory()
                result = task(driver)
            except BaseException as error:  # noqa: BLE001 - reported through the future
                if self.stalled:
                    error = BrowserUnavailable(
                        f"The {self.name} browser stopped responding and was closed by the watchdog."
                    )
                future.set_exception(error)
            else:
                future.set_result(result)
            finally:
                self._busy_since = None
                if driver is not None:
                    try:
                        driver.close()
                    except Exception:
                        logger.exception("Closing the %s browser failed", self.name)

    def shutdown(self) -> None:
        """Cancel queued tasks and stop the thread after the current task. Doesn't wait."""
        with self._lock:
            self._closed = True
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item[0].cancel()
            self._queue.put(None)


class BrowserPool:
    """The browser workers plus where each platform's profile lives."""

    def __init__(self, profiles_dir: Path, driver_factory: DriverFactory, *, stall_seconds: float = 300.0,
                 killer: Callable[[str], int] = kill_browsers):
        self.profiles_dir = profiles_dir
        self.driver_factory = driver_factory
        self.stall_seconds = stall_seconds
        self._killer = killer
        self._workers: dict[str, BrowserWorker] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    def _browser_marker(self, name: str) -> str:
        # Logged-in browsers run on their profile folder; guest browsers on a Playwright temp profile.
        return "playwright_chromiumdev_profile" if name == GUEST else f"--user-data-dir={self.profiles_dir / name}"

    def check_stalls(self) -> list[str]:
        """Close the browser of every worker idle for longer than ``stall_seconds``."""
        stopped = []
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            idle = worker.idle_seconds()
            if idle is not None and idle > self.stall_seconds and not worker.stalled:
                worker.stalled = True
                killed = self._killer(self._browser_marker(worker.name))
                logger.warning(
                    "The %s browser made no progress for %.0f s; closed %d browser process(es).", worker.name, idle, killed
                )
                stopped.append(worker.name)
        return stopped

    def _watch(self) -> None:
        while not self._stop.wait(20):
            try:
                self.check_stalls()
            except Exception:  # noqa: BLE001 - keep watching
                logger.exception("Browser watchdog check failed")

    def profile_dir(self, platform: Platform) -> Path:
        return self.profiles_dir / Platform(platform).value

    def worker(self, name: str) -> BrowserWorker:
        with self._lock:
            if name not in self._workers:
                self._workers[name] = BrowserWorker(name, self.driver_factory)
            if self._watchdog is None and self.stall_seconds > 0:
                self._watchdog = threading.Thread(target=self._watch, name="browser-watchdog", daemon=True)
                self._watchdog.start()
            return self._workers[name]

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.shutdown()
