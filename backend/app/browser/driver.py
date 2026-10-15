"""The browser interface the scanners and the login flow use.

The real implementation is ``app.browser.playwright_driver.PlaywrightDriver``; tests use a fake.
A driver and its sessions belong to one thread (sync Playwright objects aren't thread-safe), which
is why every browser task runs on a ``BrowserWorker`` (``app.browser.worker``).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class BrowserUnavailable(RuntimeError):
    """The browser couldn't start (not installed, profile in use, ...). The message says why."""


class BrowserTaskCancelled(RuntimeError):
    """A queued browser task was cancelled before it started (e.g. the backend is shutting down)."""


@dataclass
class CapturedResponse:
    """A network response captured during ``goto`` (e.g. a site's own JSON search API)."""

    url: str
    status: int
    body: str


@dataclass
class PageResult:
    url: str  # final URL, after redirects
    status: int | None  # HTTP status of the main document, when known
    html: str
    captured: list[CapturedResponse] = field(default_factory=list)


@dataclass
class ApiResponse:
    status: int
    url: str  # final URL (shows a redirect to a login page)
    json: object | None = None  # parsed body when it was JSON, else None
    text: str = ""  # the body as text (``resolve_link`` only)


class BrowserSession(Protocol):
    """One browser window (a persistent profile or a throwaway guest context) with one page."""

    def goto(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        capture: str | None = None,
        timeout: float = 45.0,
    ) -> PageResult:
        """Load ``url``. Then wait (up to ``timeout`` seconds in total) until a CSS selector
        ``wait_for`` is on the page or a response whose URL contains ``capture`` has arrived,
        whichever comes first. Captured responses are returned in ``PageResult.captured``."""
        ...

    def scroll(self) -> None:
        """Scroll to the bottom of the page, so infinite-scroll lists load more items."""
        ...

    def html(self) -> str: ...

    def current_url(self) -> str: ...

    def cookies(self) -> list[dict]:
        """Cookies of the browser context, as dicts with at least ``name``, ``value``, ``domain``."""
        ...

    def add_cookies(self, cookies: list[dict]) -> list[str]:
        """Add cookies (Playwright's format). Returns the names of any that were rejected."""
        ...

    def clear_cookies(self, domain: str) -> None:
        """Remove the cookies of ``domain`` and its subdomains."""
        ...

    def cookie_value(self, name: str, domain: str) -> str | None:
        """One cookie's value (for building a request header). Never logged."""
        ...

    def api_get(self, url: str, headers: dict[str, str]) -> "ApiResponse":
        """GET ``url`` with the context's cookies (an authenticated API call)."""
        ...

    def resolve_link(self, url: str) -> "ApiResponse":
        """Follow a short link (e.g. lnkd.in) with a plain request, without loading it in the page:
        the final URL after redirects and the start of its body."""
        ...

    def screenshot(self, path) -> bool:
        """Save a screenshot of the current page. Returns whether it worked."""
        ...

    def title(self) -> str:
        ...

    def click(self, selector: str) -> bool:
        """Click the first visible element matching the CSS selector list. False if there is none."""
        ...

    def close_extra_pages(self) -> int:
        """Close tabs a click opened (e.g. a company site), keeping the current page."""
        ...

    def type_text(self, selector: str, text: str) -> bool:
        """Type ``text`` into the first visible element matching the CSS selector list (an input, a
        textarea or a contenteditable box such as Naukri's chatbot), replacing what it held, with
        real key presses. Returns whether the element now holds the text."""
        ...

    def scroll_list(self, item_selector: str, steps: int = 8) -> bool:
        """Scroll the scrollable box holding the first element that matches ``item_selector`` down,
        step by step with short pauses, until its end (lazy lists render more items as they come
        into view). False when no element matches."""
        ...

    def click_role(self, role: str, name: str, *, within: str | None = None) -> bool:
        """Click the first visible element with ARIA ``role`` whose accessible name matches the
        regular expression ``name`` (case-insensitive), searching inside the elements matching the
        CSS selector list ``within`` when given. False when there is none."""
        ...

    def form_fields(self, scope: str, hints: dict) -> list[dict]:
        """The form fields inside the first element matching the CSS selector list ``scope`` (e.g.
        the current step of a modal form), in page order. Each is a dict with ``id`` (a handle for
        ``fill_field``), ``label``, ``type`` (text, numeric, textarea, select, radio, checkbox,
        combobox, date), ``required``, ``options``, ``value``, ``error`` (the inline error shown
        under it), ``dom_id`` and ``checked``. ``hints`` holds the site's selectors: ``container``
        (one question's wrapper), ``error``, ``skip`` (sections to leave alone, e.g. resume
        upload), ``suggestions`` (typeahead options) and ``placeholder_options``."""
        ...

    def form_state(self, scope: str, hints: dict) -> dict | None:
        """Everything about a modal form in one page round trip with a time limit (busy pages can
        make other page calls wait for minutes). A dict with:

        - ``present``: whether an element matching the CSS selector list ``scope`` is shown
        - ``fields``: as ``form_fields`` (empty when not present)
        - ``buttons``: the visible buttons of the dialog around the scope (``hints["dialog"]``, a
          selector list; the scope itself when it isn't in a dialog), each ``{"id", "label"
          (aria-label), "text", "disabled"}``; ``id`` is a handle for ``click_handle``
        - ``text``: the dialog's text; ``header``: its ``hints["header"]`` element's text;
          ``progress``: its progress bar value and "1/5 pages"-style text
        - ``dialogs``: the page's other open dialogs, each ``{"text", "buttons"}``
        - ``page_hits``: which of ``hints["watch_texts"]`` (lowercase) the page's text contains

        None when the page didn't answer in time."""
        ...

    def click_handle(self, handle: str) -> bool:
        """Click a button ``form_state`` returned, by its ``id``, from the page's own script. False
        when it's gone, disabled, or the page didn't answer in time."""
        ...

    def click_button(self, name: str, *, within: str | None = None) -> bool:
        """Click (from the page's own script, with a time limit) the first visible button or link
        whose aria-label or text matches the regular expression ``name`` (case-insensitive), among
        the elements matching the CSS selector list ``within`` and their descendants, or on the
        whole page. False when there is none or the page didn't answer in time."""
        ...

    def fill_field(self, field: dict, value: str, hints: dict) -> bool:
        """Set one field returned by ``form_fields``: type text, choose a select / radio option by
        its text, tick or untick a checkbox (``"true"`` / ``"false"``, or option texts separated by
        newlines for a group), or type into a typeahead and pick its first suggestion. Returns
        whether the field now holds the value."""
        ...

    def is_closed(self) -> bool:
        """True when the user closed the window (or the browser went away)."""
        ...

    def wait(self, seconds: float) -> None:
        """Sleep while letting the browser process its events."""
        ...

    def close(self) -> None: ...


class BrowserDriver(Protocol):
    """Opens browser sessions. Created and used on a single worker thread, closed after each task."""

    def open(self, *, profile_dir: Path | None, headless: bool) -> BrowserSession:
        """Open a window. ``profile_dir`` is a persistent profile (saved login); ``None`` opens a
        throwaway guest context with no cookies."""
        ...

    def close(self) -> None: ...


def has_cookie(cookies: Sequence[dict], names: Sequence[str], domain: str) -> bool:
    """Whether a non-empty cookie with one of ``names`` exists for ``domain`` (or a subdomain)."""
    for cookie in cookies:
        if cookie.get("name") in names and cookie.get("value"):
            if cookie.get("domain", "").lstrip(".").endswith(domain):
                return True
    return False
