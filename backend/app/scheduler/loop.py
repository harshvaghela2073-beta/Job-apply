"""The background schedule while ``SCAN_ENABLED``: each scan group (LinkedIn, Naukri) runs on its own
interval, ``SCAN_INTERVAL_MINUTES`` until one is set in the app (``set_interval``; docs/API_CONTRACT.md,
"Separate LinkedIn and Naukri scans").

A group's next run is its interval after its previous run finished (manual runs count too). After a
(re)start it is planned from that group's last finished run in the database, or 1 minute after
startup if there was none within its interval, so ``--reload`` restarts don't start a scan storm.
"""

import asyncio
import contextlib
import logging
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta

from app.models import Platform
from app.scheduler.runner import ScanService
from app.timeutils import utcnow

logger = logging.getLogger(__name__)

STARTUP_DELAY = timedelta(minutes=1)


def initial_next_run(
    last_finished: datetime | None,
    now: datetime,
    interval: timedelta,
    startup_delay: timedelta = STARTUP_DELAY,
) -> datetime:
    if last_finished is not None and now - last_finished < interval:
        return last_finished + interval
    return now + startup_delay


class Scheduler:
    def __init__(self, scans: ScanService, *, enabled: bool, intervals: Mapping[Platform, int]):
        """``intervals``: minutes between runs of each group (every group needs one)."""
        self.scans = scans
        self.enabled = enabled
        self.tick_seconds = 5.0
        # Guards the intervals and next runs: changed by requests, the loop and the scan threads.
        self._lock = threading.Lock()
        self._intervals = {platform: timedelta(minutes=intervals[platform]) for platform in Platform}
        self._next: dict[Platform, datetime | None] = dict.fromkeys(Platform)
        self._task: asyncio.Task | None = None
        scans.on_finished = self._run_finished

    def interval(self, platform: Platform | str) -> timedelta:
        with self._lock:
            return self._intervals[Platform(platform)]

    def interval_minutes(self, platform: Platform | str) -> int:
        return int(self.interval(platform).total_seconds() // 60)

    def next_run_at(self, platform: Platform | str | None = None) -> datetime | None:
        """This group's next run, or the earliest of all groups when ``platform`` is None. None while
        that group runs (or all of them do) or when scanning is disabled."""
        if not self.enabled:
            return None
        groups = list(Platform) if platform is None else [Platform(platform)]
        with self._lock:
            planned = {group: self._next[group] for group in groups}
        upcoming = [at for group, at in planned.items() if at is not None and not self.scans.running_for(group)]
        return min(upcoming, default=None)

    def set_interval(self, platform: Platform | str, minutes: int) -> None:
        """A new interval for this group (from the app). A started schedule plans the group again from
        its last finished run; a running group plans its next run itself when it finishes."""
        platform = Platform(platform)
        with self._lock:
            self._intervals[platform] = interval = timedelta(minutes=minutes)
        if self._task is not None and self.enabled and not self.scans.running_for(platform):
            self._plan(platform, interval)

    def _plan(self, platform: Platform, interval: timedelta) -> None:
        planned = initial_next_run(self.scans.last_finished_at(platform), utcnow(), interval)
        with self._lock:
            self._next[platform] = planned
        logger.info("Scanning %s every %s; next run at %s", platform.value, interval, planned.isoformat())

    def _run_finished(self, platform: Platform, finished_at: datetime) -> None:
        if self.enabled:
            with self._lock:
                self._next[platform] = finished_at + self._intervals[platform]

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        for platform in Platform:
            self._plan(platform, self.interval(platform))
        self._task = asyncio.create_task(self._loop(), name="scan-scheduler")

    def _due(self) -> list[Platform]:
        now = utcnow()
        with self._lock:
            return [group for group, at in self._next.items() if at is not None and now >= at]

    async def _loop(self) -> None:
        while True:
            try:
                for platform in self._due():
                    if not self.scans.running_for(platform) and self.scans.start(platform):
                        logger.info("Scheduled %s scan run started", platform.value)
            except Exception:  # keep the schedule alive
                logger.exception("Starting a scheduled scan failed")
            await asyncio.sleep(self.tick_seconds)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
