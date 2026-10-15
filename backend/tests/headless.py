"""A local headless Chromium for testing the driver's in-page scripts on synthetic HTML
(``page.set_content``). No network (every request is aborted), no saved profile: a throwaway context.
Tests using it are skipped when no Chromium can be started."""

import pytest


@pytest.fixture(scope="module")
def chromium():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover
        pytest.skip("Playwright is not installed")
    playwright = sync_playwright().start()
    browser = None
    errors = []
    # Playwright's own Chromium first; else the installed Chrome (a fresh temporary profile).
    for options in ({}, {"channel": "chrome"}):
        try:
            browser = playwright.chromium.launch(headless=True, **options)
            break
        except Exception as error:  # noqa: BLE001 - not installed / can't start here
            errors.append(str(error).splitlines()[0])
    if browser is None:
        playwright.stop()
        pytest.skip("No headless Chromium available: " + "; ".join(errors))
    yield browser
    browser.close()
    playwright.stop()


@pytest.fixture
def page_session(chromium):
    """A ``PlaywrightSession`` on an empty page; ``session.show(html)`` loads synthetic HTML."""
    from app.browser.playwright_driver import PlaywrightSession

    context = chromium.new_context()
    context.route("**/*", lambda route: route.abort())  # never the network
    page = context.new_page()
    browser_session = PlaywrightSession(context)
    browser_session.show = lambda html: page.set_content(html)  # type: ignore[attr-defined]
    browser_session.page = page  # type: ignore[attr-defined]
    yield browser_session
    context.close()
