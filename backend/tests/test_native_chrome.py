"""Chrome starts as a native Apple-silicon app when the backend's Python runs under Rosetta."""

import os

from app.browser import playwright_driver
from app.browser.playwright_driver import NATIVE_CHROME, PlaywrightDriver


def test_rosetta_python_starts_native_chrome(monkeypatch, tmp_path):
    chrome = tmp_path / "Google Chrome"
    chrome.write_text("")
    monkeypatch.setattr(playwright_driver, "CHROME_APP", chrome)
    monkeypatch.setattr(playwright_driver, "under_rosetta", lambda: True)
    options = PlaywrightDriver()._launch_options(True)
    assert "channel" not in options and options["executable_path"] == str(NATIVE_CHROME)
    assert options["headless"] is True and options["args"] == playwright_driver.LAUNCH_ARGS


def test_native_python_or_other_channel_keeps_the_channel(monkeypatch, tmp_path):
    chrome = tmp_path / "Google Chrome"
    chrome.write_text("")
    monkeypatch.setattr(playwright_driver, "CHROME_APP", chrome)
    monkeypatch.setattr(playwright_driver, "under_rosetta", lambda: False)
    assert PlaywrightDriver()._launch_options(False)["channel"] == "chrome"
    monkeypatch.setattr(playwright_driver, "under_rosetta", lambda: True)
    options = PlaywrightDriver(channel="msedge")._launch_options(True)
    assert options["channel"] == "msedge" and "executable_path" not in options


def test_launcher_script_is_executable_and_passes_the_arguments():
    assert os.access(NATIVE_CHROME, os.X_OK)
    text = NATIVE_CHROME.read_text()
    assert "arch -arm64" in text and '"$@"' in text


def test_under_rosetta_answers_a_bool():
    assert isinstance(playwright_driver.under_rosetta(), bool)
