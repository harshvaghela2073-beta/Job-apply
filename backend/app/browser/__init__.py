"""Browser automation: a driver interface, its Playwright implementation and worker threads."""

from app.browser.driver import (
    BrowserDriver,
    BrowserSession,
    BrowserTaskCancelled,
    BrowserUnavailable,
    CapturedResponse,
    PageResult,
)
from app.browser.worker import GUEST, BrowserPool, BrowserWorker

__all__ = [
    "GUEST",
    "BrowserDriver",
    "BrowserPool",
    "BrowserSession",
    "BrowserTaskCancelled",
    "BrowserUnavailable",
    "BrowserWorker",
    "CapturedResponse",
    "PageResult",
]
