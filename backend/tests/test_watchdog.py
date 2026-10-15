import threading
import time

from app.browser.worker import GUEST, BrowserPool, _descendant_pids, beat


class SlowDriver:
    def close(self):
        pass


def test_a_silent_task_is_stopped_and_reported(tmp_path):
    killed: list[str] = []
    release = threading.Event()

    def killer(marker: str) -> int:
        killed.append(marker)
        release.set()  # killing the browser makes the stuck page call fail
        return 1

    pool = BrowserPool(tmp_path, SlowDriver, stall_seconds=0.05, killer=killer)
    pool._stop.set()  # no background thread: call check_stalls() by hand

    def stuck(driver):
        beat()
        release.wait(5)
        raise RuntimeError("Target page, context or browser has been closed")

    future = pool.worker("linkedin").submit(stuck)
    time.sleep(0.2)
    assert pool.check_stalls() == ["linkedin"]
    assert killed == [f"--user-data-dir={tmp_path / 'linkedin'}"]
    error = future.exception(timeout=5)
    assert "stopped responding and was closed by the watchdog" in str(error)
    assert pool.check_stalls() == []  # idle again


def test_an_active_task_is_left_alone(tmp_path):
    pool = BrowserPool(tmp_path, SlowDriver, stall_seconds=0.3, killer=lambda marker: 0)
    pool._stop.set()

    def busy(driver):
        for _ in range(8):
            beat()
            time.sleep(0.05)
        return "done"

    future = pool.worker(GUEST).submit(busy)
    for _ in range(6):
        time.sleep(0.05)
        assert pool.check_stalls() == []
    assert future.result(timeout=5) == "done"


def test_descendant_pids_finds_children(monkeypatch):
    import subprocess as sp

    class Out:
        stdout = "  10 1 python app\n  20 10 node driver\n  30 20 Google Chrome --user-data-dir=/x/linkedin\n  40 1 other\n"

    monkeypatch.setattr(sp, "run", lambda *a, **k: Out())
    assert _descendant_pids(10) == {20: "node driver", 30: "Google Chrome --user-data-dir=/x/linkedin"}
