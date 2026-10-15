"""A fake browser for tests: no real browser or network is ever used."""

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.browser.driver import ApiResponse, CapturedResponse, PageResult

Route = PageResult | Callable[[str], PageResult]


def page(url: str, html: str = "<html></html>", status: int = 200, captured=None) -> PageResult:
    return PageResult(url=url, status=status, html=html, captured=list(captured or []))


def api(url: str, body: str) -> CapturedResponse:
    return CapturedResponse(url=url, status=200, body=body)


@dataclass
class FakeBrowser:
    """Shared, scriptable state behind every session the fake driver opens.

    - ``routes``: ``(url fragment, PageResult or function)``; the first fragment found in the URL
      wins. Unknown URLs return an empty page at the same URL.
    - ``cookies``: returned by ``cookies()``; ``login_after_polls`` adds ``login_cookie`` after that
      many polls of a headed (login) window; ``close_after_polls`` closes it instead.
    """

    routes: list[tuple[str, Route]] = field(default_factory=list)
    # (url fragment -> ApiResponse or callable) for api_get.
    api_routes: list[tuple[str, object]] = field(default_factory=list)
    cookies: list[dict] = field(default_factory=list)
    screenshots: list = field(default_factory=list)
    login_cookie: dict | None = None
    login_after_polls: int | None = None
    close_after_polls: int | None = None
    visited: list[str] = field(default_factory=list)
    # (url fragment -> PageResult or callable) shown after a click on that page; no match = nothing to click.
    click_routes: list[tuple[str, Route]] = field(default_factory=list)
    clicks: list[tuple[str, str]] = field(default_factory=list)
    opened: list[tuple[Path | None, bool]] = field(default_factory=list)
    open_error: Exception | None = None
    # (url fragment -> FakeEasyApply or FakeProfile): a scripted LinkedIn job page with its Easy
    # Apply form, or a profile page with its Connect / "Add a note" dialog.
    forms: list[tuple[str, "FakeEasyApply | FakeProfile"]] = field(default_factory=list)
    list_scrolls: int = 0
    polls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def route(self, url: str) -> PageResult:
        self.visited.append(url)
        for fragment, target in self.routes:
            if fragment in url:
                return target(url) if callable(target) else target
        return page(url)

    def factory(self):
        return lambda: FakeDriver(self)


class FakeSession:
    def __init__(self, browser: FakeBrowser, profile_dir: Path | None, headless: bool):
        self.browser = browser
        self.profile_dir = profile_dir
        self.headless = headless
        self.url = "about:blank"
        self.closed = False
        self.scrolls = 0
        self._html = ""

    def goto(self, url, *, wait_for=None, capture=None, timeout=45.0) -> PageResult:
        form = self._form(url)
        if form is not None:
            self.browser.visited.append(url)
            self.url = url
            form.reset_view()
            return PageResult(url=url, status=200, html=form.html())
        result = self.browser.route(url)
        self.url = result.url
        self._html = result.html
        return result

    def _form(self, url: str | None = None) -> "FakeEasyApply | None":
        url = self.url if url is None else url
        for fragment, form in self.browser.forms:
            if fragment in url:
                return form
        return None

    def scroll(self) -> None:
        self.scrolls += 1

    def scroll_list(self, item_selector: str, steps: int = 8) -> bool:
        self.browser.list_scrolls += 1
        return True

    def click_role(self, role: str, name: str, *, within: str | None = None) -> bool:
        form = self._form()
        if form is None:
            return False
        if isinstance(form, FakeProfile):
            return form.click(role, name, within)
        return form.click(role, name)

    def form_fields(self, scope: str, hints: dict) -> list[dict]:
        form = self._form()
        return form.fields() if form is not None else []

    def form_state(self, scope: str, hints: dict) -> dict | None:
        form = self._form()
        if isinstance(form, FakeEasyApply):
            return form.state(hints)
        return {"present": False, "fields": [], "buttons": [], "text": "", "header": "", "progress": "",
                "dialogs": [], "page_hits": []}

    def click_handle(self, handle: str) -> bool:
        form = self._form()
        return isinstance(form, FakeEasyApply) and form.click_handle(handle)

    def click_button(self, name: str, *, within: str | None = None) -> bool:
        form = self._form()
        if form is None:
            return False
        if isinstance(form, FakeProfile):
            return form.click("button", name, within)
        return form.click("button", name)

    def fill_field(self, field: dict, value: str, hints: dict) -> bool:
        form = self._form()
        return form.fill(field["id"], value) if form is not None else False

    def html(self) -> str:
        form = self._form()
        if form is not None:
            return form.html()
        return self._html

    def current_url(self) -> str:
        return self.url

    def cookies(self) -> list[dict]:
        return list(self.browser.cookies)

    def add_cookies(self, cookies: list[dict]) -> list[str]:
        for cookie in cookies:
            domain = cookie.get("domain") or cookie["url"].split("/")[2]
            self.browser.cookies.append({"name": cookie["name"], "value": cookie["value"], "domain": domain})
        return []

    def clear_cookies(self, domain: str) -> None:
        self.browser.cookies = [
            c for c in self.browser.cookies
            if not (c["domain"].lstrip(".") == domain or c["domain"].endswith("." + domain))
        ]

    def cookie_value(self, name: str, domain: str) -> str | None:
        for c in self.browser.cookies:
            if c["name"] == name and c["domain"].lstrip(".").endswith(domain):
                return c["value"]
        return None

    def api_get(self, url, headers) -> ApiResponse:
        self.browser.visited.append("API " + url)
        for fragment, target in self.browser.api_routes:
            if fragment in url:
                return target(url, headers) if callable(target) else target
        return ApiResponse(status=404, url=url, json=None)

    def resolve_link(self, url) -> ApiResponse:
        """Like a page load of ``url`` in ``routes``, without counting as one (no page is opened)."""
        self.browser.visited.append("LINK " + url)
        for fragment, target in self.browser.routes:
            if fragment in url:
                result = target(url) if callable(target) else target
                return ApiResponse(status=200, url=result.url, text=result.html)
        return ApiResponse(status=404, url=url)

    def screenshot(self, path) -> bool:
        self.browser.screenshots.append(str(path))
        return True

    def click(self, selector: str) -> bool:
        for fragment, target in self.browser.click_routes:
            if fragment in self.url:
                self.browser.clicks.append((self.url, selector))
                result = target(self.url) if callable(target) else target
                self.url = result.url
                self._html = result.html
                return True
        return False

    def close_extra_pages(self) -> int:
        return 0

    def title(self) -> str:
        import re as _re
        match = _re.search(r"<title>(.*?)</title>", self._html, _re.S)
        return match.group(1).strip() if match else ""

    def is_closed(self) -> bool:
        if not self.headless:
            browser = self.browser
            browser.polls += 1
            if browser.close_after_polls is not None and browser.polls >= browser.close_after_polls:
                self.closed = True
            elif (
                browser.login_after_polls is not None
                and browser.polls >= browser.login_after_polls
                and browser.login_cookie not in browser.cookies
            ):
                browser.cookies.append(browser.login_cookie)
        return self.closed

    def wait(self, seconds: float) -> None:
        threading.Event().wait(min(seconds, 0.01))

    def close(self) -> None:
        self.closed = True


class FakeDriver:
    def __init__(self, browser: FakeBrowser):
        self.browser = browser
        self.sessions: list[FakeSession] = []

    def open(self, *, profile_dir, headless):
        if self.browser.open_error is not None:
            raise self.browser.open_error
        if profile_dir is not None:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
        self.browser.opened.append((profile_dir, headless))
        session = FakeSession(self.browser, profile_dir, headless)
        self.sessions.append(session)
        return session

    def close(self) -> None:
        for session in self.sessions:
            session.close()


# ---------------------------------------------------------------------------
# A scripted LinkedIn job page with an Easy Apply form


@dataclass
class FakeField:
    label: str
    type: str = "text"
    required: bool = True
    options: list[str] = field(default_factory=list)
    value: str = ""
    dom_id: str = ""
    checked: bool = False
    # Returns LinkedIn's inline error for a value ("" when the value is accepted).
    validate: Callable[[str], str] | None = None
    fill_ok: bool = True
    error: str = ""

    def empty(self) -> bool:
        if self.type == "checkbox" and not self.options:
            return not self.checked
        return not self.value.strip()


@dataclass
class FakeStep:
    fields: list[FakeField]
    button: str = "next"  # next | review | submit
    header: str = "Contact info"


BUTTON_LABELS = {"next": "Continue to next step", "review": "Review your application", "submit": "Submit application"}
# The 2026-10-14 layout: footer buttons have only their text.
SDUI_BUTTON_TEXTS = {"next": "Next", "review": "Review", "submit": "Submit application"}


@dataclass
class FakeEasyApply:
    """The job page shows ``job_html`` (with an Easy Apply button unless the test says otherwise);
    clicking "Easy Apply" opens a modal that walks through ``steps``. Next / Review validate the
    step like LinkedIn: empty required fields and ``validate`` errors keep the step open with an
    inline error. Dismiss asks "Save this application?" (Discard / Save)."""

    steps: list[FakeStep]
    job_html: str = ""
    open: bool = False
    index: int = 0
    submitted: bool = False
    discarded: bool = False
    save_prompt: bool = False
    success_on_submit: bool = True
    # What the modal says instead of the form (e.g. the daily limit message).
    modal_message: str = ""
    clicks: list[str] = field(default_factory=list)
    fills: list[tuple[str, str]] = field(default_factory=list)
    # The checkbox values at the moment Submit was clicked (label -> checked).
    checked_at_submit: dict[str, bool] = field(default_factory=dict)
    # The 2026-10-14 layout: footer buttons by text only ("Next"), the confirmation in a <dialog>.
    sdui: bool = False
    # The page didn't answer this many form reads (a busy page): form_state returns None.
    silent_reads: int = 0
    reads: int = 0

    def _footer(self, kind: str) -> str:
        return (SDUI_BUTTON_TEXTS if self.sdui else BUTTON_LABELS)[kind]

    def _button(self, name: str) -> dict:
        """A button as the driver's form_state returns it: SDUI footer buttons have no aria-label."""
        text_only = self.sdui and name in SDUI_BUTTON_TEXTS.values()
        return {"id": "b:" + name, "label": "" if text_only else name, "text": name if text_only else "", "disabled": False}

    def state(self, hints: dict | None = None) -> dict | None:
        """What BrowserSession.form_state returns for this page."""
        self.reads += 1
        if self.silent_reads > 0:
            self.silent_reads -= 1
            return None
        dialog_buttons = [b for b in self._buttons() if b not in ("Discard", "Save")] if self.open else []
        dialogs = []
        if self.save_prompt:
            dialogs.append({"text": "Save this application? Discard Save",
                            "buttons": [self._button("Discard"), self._button("Save")]})
        text = ""
        if self.open:
            text = ("Application sent Your application was sent to Acme!" if self.submitted
                    else self.modal_message or f"{self.step.header} {self.index + 1}/{len(self.steps)} pages")
        watch = [t.lower() for t in (hints or {}).get("watch_texts", [])]
        page_text = self.html().lower()
        return {
            "present": self.open, "fields": self.fields(), "buttons": [self._button(b) for b in dialog_buttons],
            "text": text, "header": "" if self.sdui or not self.open else self.step.header,
            "progress": str(self.index * 25) if self.open else "", "dialogs": dialogs,
            "page_hits": [t for t in watch if t in page_text],
        }

    def click_handle(self, handle: str) -> bool:
        if not handle.startswith("b:"):
            return False
        return self.click("button", f"^{re.escape(handle[2:])}$")

    def reset_view(self) -> None:
        """A fresh page load: the form is closed and starts again at its first step."""
        self.open = False
        self.save_prompt = False
        self.index = 0
        for step in self.steps:
            for f in step.fields:
                f.error = ""

    @property
    def step(self) -> FakeStep:
        return self.steps[self.index]

    def _field_id(self, index: int) -> str:
        return f"s{self.index}-f{index}"

    def fields(self) -> list[dict]:
        if not self.open or self.submitted or self.modal_message:
            return []
        out = []
        for i, f in enumerate(self.step.fields):
            value = f.value
            if f.type == "checkbox" and not f.options:
                value = "true" if f.checked else "false"
            out.append({
                "id": self._field_id(i), "label": f.label, "type": f.type, "required": f.required,
                "options": list(f.options), "value": value, "error": f.error, "dom_id": f.dom_id,
                "checked": f.checked,
            })
        return out

    def fill(self, field_id: str, value: str) -> bool:
        if not self.open:
            return False
        for i, f in enumerate(self.step.fields):
            if self._field_id(i) != field_id:
                continue
            self.fills.append((f.label, value))
            if not f.fill_ok:
                return False
            if f.type in ("select", "radio") and value not in f.options:
                return False
            if f.type == "checkbox" and not f.options:
                f.checked = value == "true"
            else:
                f.value = value
            return True
        return False

    def _buttons(self) -> list[str]:
        if self.save_prompt:
            return ["Discard", "Save"]
        if not self.open:
            return ["Easy Apply to Backend Developer at Acme"] if "Easy Apply" in self.job_html else []
        if self.submitted or self.modal_message:
            return ["Dismiss", "Done"]
        return ["Dismiss", self._footer(self.step.button)]

    def click(self, role: str, pattern: str) -> bool:
        if role != "button":
            return False
        name = next((b for b in self._buttons() if re.search(pattern, b, re.I)), None)
        if name is None:
            return False
        self.clicks.append(name)
        if name.startswith("Easy Apply"):
            self.open = True
        elif name == "Discard":
            self.discarded, self.save_prompt, self.open = True, False, False
        elif name == "Save":
            self.save_prompt, self.open = False, False
        elif name in ("Dismiss", "Done"):
            if self.submitted or self.modal_message:
                self.open = False
            else:
                self.save_prompt = True
        elif name == self._footer("submit"):
            self.checked_at_submit = {f.label: f.checked for f in self.step.fields if f.type == "checkbox"}
            self.submitted = self.success_on_submit
        else:  # next / review
            if self._validate():
                self.index += 1
        return True

    def _validate(self) -> bool:
        ok = True
        for f in self.step.fields:
            f.error = ""
            if f.required and f.empty():
                f.error = "Please enter a valid answer" if f.type in ("text", "numeric", "textarea") else "Please make a selection"
            elif f.validate is not None and not f.empty():
                f.error = f.validate(f.value)
            ok = ok and not f.error
        return ok

    def html(self) -> str:
        page = self.job_html
        if self.open:
            if self.submitted:
                body = "<h2>Application sent</h2><p>Your application was sent to Acme!</p>"
            elif self.modal_message:
                body = f"<p>{self.modal_message}</p>"
            else:
                body = f"<h3 class='t-16'>{self.step.header}</h3><progress value='{self.index * 25}'></progress>"
            buttons = "".join(f'<button aria-label="{b}">{b}</button>' for b in self._buttons() if b not in ("Discard", "Save"))
            page += f'<div class="jobs-easy-apply-modal" role="dialog">{body}{buttons}</div>'
        if self.save_prompt:
            page += '<div role="alertdialog"><h2>Save this application?</h2><button>Discard</button><button>Save</button></div>'
        return f"<html><body>{page}</body></html>"



# ---------------------------------------------------------------------------
# A scripted LinkedIn profile page with the Connect / "Add a note" invitation dialog


@dataclass
class FakeProfile:
    """A profile whose top card shows ``connect``:

    - ``"primary"``: "Connect" (aria-label "Invite <name> to connect"); ``"more"``: only under
      "More actions"; ``None``: no Connect at all (connected / follow-only); ``"pending"``.

    Clicking Connect does ``on_connect``: ``"dialog"`` (Add a note / Send without a note),
    ``"weekly_limit"``, ``"email"`` (LinkedIn wants their email) or ``"instant"`` (sent at once).
    "Add a note" does ``on_add_note``: ``"box"`` or ``"note_limit"`` (Premium offer). "Send" does
    ``on_send``: ``"sent"``, ``"weekly_limit"``, ``"error"`` or ``"nothing"``. A "People also
    viewed" card has another person's Connect button, outside the top card. ``within`` scopes are
    honoured: the top card, the dialog, or (None) the whole page."""

    name: str = "Priya Sharma"
    connect: str | None = "primary"
    on_connect: str = "dialog"
    on_add_note: str = "box"
    on_send: str = "sent"
    maxlength: int = 300
    state: str = "page"  # page | menu | dialog | note | weekly_limit | email | note_limit
    note: str = ""
    sent: bool = False
    sent_without_note: bool = False
    toast: str = ""
    clicks: list[str] = field(default_factory=list)
    fills: list[str] = field(default_factory=list)

    OTHER = "Invite Rahul Verma to connect"

    def reset_view(self) -> None:
        self.state, self.toast = "page", ""

    def _top(self) -> list[str]:
        if self.sent or self.connect == "pending":
            return [f"Pending, click to withdraw invitation sent to {self.name}", "Message", "More actions"]
        buttons = {
            "primary": ["Message", f"Invite {self.name} to connect", "More actions"],
            "more": ["Follow", "Message", "More actions"],
            None: ["Message", "More actions"],
        }[self.connect]
        return list(buttons)

    def _menu(self) -> list[str]:
        if self.state != "menu":
            return []
        connect = [f"Invite {self.name} to connect"] if self.connect == "more" else ["Remove connection"]
        return [*connect, "Save to PDF"]

    def _dialog(self) -> list[str]:
        return {
            "dialog": ["Dismiss", "Add a note", "Send without a note"],
            "note": ["Dismiss", "Cancel", "Send invitation"],
            "weekly_limit": ["Dismiss", "Got it"],
            "email": ["Dismiss", "Send without a note"],
            "note_limit": ["Dismiss", "Try Premium for ₹0", "Send without a note"],
        }.get(self.state, [])

    def click(self, role: str, pattern: str, within: str | None = None) -> bool:
        if role not in ("button", "menuitem"):
            return False
        if within and "dialog" in within:
            candidates = self._dialog()
        elif within:
            candidates = self._top() + self._menu()
        else:
            candidates = [self.OTHER] + self._top() + self._menu() + self._dialog()
        label = next((b for b in candidates if re.search(pattern, b, re.I)), None)
        if label is None:
            return False
        self.clicks.append(label)
        if label == self.OTHER:
            pass  # someone else's Connect: the flow must never get here
        elif label == f"Invite {self.name} to connect":
            if self.on_connect == "instant":
                self.sent, self.state, self.toast = True, "page", "Invitation sent"
                self.sent_without_note = True
            else:
                self.state = self.on_connect
        elif label == "More actions":
            self.state = "page" if self.state == "menu" else "menu"
        elif label == "Add a note":
            self.state = "note" if self.on_add_note == "box" else "note_limit"
        elif label == "Send without a note":
            self.sent = self.sent_without_note = True
            self.state = "page"
        elif label == "Send invitation":
            if self.on_send == "sent":
                self.sent, self.state = True, "page"
                self.toast = f"Your invitation to {self.name.split()[0]} was sent."
            elif self.on_send == "weekly_limit":
                self.state = "weekly_limit"
            elif self.on_send == "error":
                self.state, self.toast = "page", "Invitation not sent. Something went wrong."
        elif label in ("Dismiss", "Cancel", "Got it"):
            self.state = "page"
        return True

    def fields(self) -> list[dict]:
        if self.state != "note":
            return []
        return [{
            "id": "note", "label": "Add a note", "type": "textarea", "required": False, "options": [],
            "value": self.note, "error": "", "dom_id": "custom-message", "checked": False,
        }]

    def fill(self, field_id: str, value: str) -> bool:
        if self.state != "note" or field_id != "note":
            return False
        self.fills.append(value)
        self.note = value[: self.maxlength]
        return self.note == value

    def html(self) -> str:
        def buttons(labels, tag="button"):
            role = ' role="button"' if tag != "button" else ""
            return "".join(f'<{tag}{role} aria-label="{b}"><span>{b.split(",")[0]}</span></{tag}>' for b in labels)

        top = (
            f'<section class="artdeco-card pv-top-card"><h1 class="text-heading-xlarge">{self.name}</h1>'
            f'<div class="pvs-profile-actions">{buttons(self._top())}'
            f'<div class="artdeco-dropdown__content">{buttons(self._menu(), "div")}</div></div></section>'
        )
        aside = f'<aside><section class="pv-browsemap"><button aria-label="{self.OTHER}">Connect</button></section></aside>'
        texts = {
            "dialog": "<h2>Add a note to your invitation?</h2><p>Personalize your invitation by adding a note.</p>",
            "note": f'<label for="custom-message">Add a note</label><textarea name="message" id="custom-message" maxlength="{self.maxlength}">{self.note}</textarea>',
            "weekly_limit": "<h2>You’ve reached the weekly invitation limit</h2><p>Please try again next week.</p>",
            "email": "<p>To verify this member knows you, please enter their email to connect.</p><input type='email'>",
            "note_limit": "<h2>You've used all your personalized invitations for this month</h2>",
        }
        dialog = ""
        if self.state in texts:
            dialog = f'<div role="dialog" class="artdeco-modal send-invite">{texts[self.state]}{buttons(self._dialog())}</div>'
        toast = f'<div class="artdeco-toast-item"><p>{self.toast}</p></div>' if self.toast else ""
        return f"<html><body><main>{top}{aside}</main>{dialog}{toast}</body></html>"


# ---------------------------------------------------------------------------
# A fake SMTP server (no network)


@dataclass
class FakeSMTP:
    """Records what the mailer does: ``factory(host, port, timeout)`` opens a "connection".

    ``login_error`` / ``connect_error`` raise on login / connect; ``refuse`` lists recipients the
    server refuses."""

    login_error: Exception | None = None
    connect_error: Exception | None = None
    refuse: set[str] = field(default_factory=set)
    calls: list[tuple] = field(default_factory=list)
    sent: list = field(default_factory=list)

    def factory(self, host: str, port: int, timeout: float) -> "FakeSMTP":
        if self.connect_error is not None:
            raise self.connect_error
        self.calls.append(("connect", host, port))
        return self

    def ehlo(self, name: str = ""):
        self.calls.append(("ehlo",))

    def starttls(self, *, context=None):
        self.calls.append(("starttls", context is not None))

    def login(self, user: str, password: str):
        self.calls.append(("login", user, password))
        if self.login_error is not None:
            raise self.login_error

    def send_message(self, msg) -> dict:
        import smtplib

        to = str(msg["To"])
        if to in self.refuse:
            raise smtplib.SMTPRecipientsRefused({to: (550, b"5.1.1 The email account that you tried to reach does not exist.")})
        self.calls.append(("send", to))
        self.sent.append(msg)
        return {}

    def quit(self):
        self.calls.append(("quit",))

    def close(self):
        self.calls.append(("close",))
