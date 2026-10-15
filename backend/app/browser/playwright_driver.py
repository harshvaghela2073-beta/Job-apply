"""Playwright implementation of ``BrowserDriver``, using the installed Google Chrome by default.

- Scans run headless. Naukri blocks headless Chrome when the user agent says "HeadlessChrome", so
  headless sessions use the normal Chrome user agent of the same browser.
- Login windows are headed (visible) so the user can type passwords, OTPs and solve CAPTCHAs.
- All sessions use an Indian English locale and the Asia/Kolkata timezone.
- When the backend's Python is an Intel build on an Apple-silicon Mac (it runs under Rosetta),
  Chrome is started as a native arm64 app through ``chrome-native.sh``. Otherwise Chrome inherits
  Rosetta, and LinkedIn's pages keep a CPU core busy for minutes (found live on 2026-10-14).
"""

import ctypes
import ctypes.util
import functools
import logging
import random
import re
import sys
import threading
import time
from pathlib import Path

from app.browser.driver import ApiResponse, BrowserUnavailable, CapturedResponse, PageResult
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

LOCALE = "en-IN"
TIMEZONE = "Asia/Kolkata"
VIEWPORT = {"width": 1366, "height": 768}
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# Drops the "Chrome is being controlled by automated test software" bar.
IGNORE_DEFAULT_ARGS = ["--enable-automation"]
POLL_SECONDS = 0.25
CHROME_APP = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
NATIVE_CHROME = Path(__file__).with_name("chrome-native.sh")

# Page scripts run through PlaywrightSession._evaluate get this long per try. page.evaluate itself
# has no time limit and waits forever on a page whose main thread is busy (LinkedIn, 2026-10-14).
EVAL_TIMEOUT_SECONDS = 20.0  # LinkedIn's job page keeps its main thread busy; page calls queue behind it

# Reads the form fields inside a scope (see BrowserSession.form_fields). Each control gets a
# data-ja-field attribute (radio / checkbox options also data-ja-opt) that fill_field finds it by.
# ``root`` (optional) is the element to read, already found by the caller (FORM_STATE_JS).
FORM_FIELDS_JS = r"""
({ scope, hints, root: given }) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const dedupe = (t) => {
    t = clean(t);
    const words = t.split(' ');
    const half = Math.floor(words.length / 2);
    if (words.length % 2 === 0 && half > 0 && words.slice(0, half).join(' ') === words.slice(half).join(' ')) {
      return words.slice(0, half).join(' ');
    }
    return t;
  };
  const textOf = (el) => {
    if (!el) return '';
    const copy = el.cloneNode(true);
    copy.querySelectorAll('.visually-hidden, .a11y-text, .artdeco-inline-feedback, select, input, textarea')
      .forEach((n) => n.remove());
    return dedupe(copy.textContent).replace(/\s*\*+$/, '') || dedupe(el.textContent).replace(/\s*\*+$/, '');
  };
  const placeholders = (hints.placeholder_options || 'select an option').toLowerCase().split('|');
  const isPlaceholder = (t) => !clean(t) || placeholders.includes(clean(t).toLowerCase());
  const hasControls = (el) => el.querySelector('input, select, textarea, button');
  let root = given || null;
  for (const part of given ? [] : scope.split(',')) {
    const found = Array.from(document.querySelectorAll(part.trim()));
    root = found.find(hasControls) || null;
    if (root) break;
  }
  if (!root) return [];
  const visible = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  const skipRoots = hints.skip ? Array.from(root.querySelectorAll(hints.skip)) : [];
  const inSkip = (el) => skipRoots.some((n) => n.contains(el));
  // Generated class names (LinkedIn's 2026 layout): the smallest ancestor holding the control's
  // <label for=id> and no other control.
  const labelBox = (el) => {
    if (!el.id) return null;
    let node = el.parentElement;
    for (let i = 0; node && node !== root && i < 5; i++, node = node.parentElement) {
      if (node.querySelector('label[for="' + CSS.escape(el.id) + '"]')) {
        return node.querySelectorAll('input, select, textarea').length === 1 ? node : null;
      }
    }
    return null;
  };
  // One question's wrapper; a lone control (e.g. "Follow company") is its own box, so the
  // required flag and inline error of other questions don't leak into it.
  const containerOf = (el) => {
    const box = (hints.container && el.closest(hints.container)) || el.closest('fieldset');
    if (box) return box;
    const own = labelBox(el);
    if (own) return own;
    const parent = el.parentElement;
    if (parent && parent.querySelectorAll('input, select, textarea').length === 1) return parent;
    return el;
  };
  // LinkedIn's 2026-10-14 layout: the control's aria-describedby helper holds a character counter
  // ("6/20", in an aria-live part) and, once an answer is rejected, a red line such as "Invalid input".
  const COUNTER = /\b\d+\s*\/\s*\d+\b|\b\d+ of \d+ characters\b/gi;
  // Wording that is an error even when not shown in red (a grey hint such as "Please enter in lakhs" isn't).
  const ERROR_WORDS = /\binvalid\b|\bis required\b|\benter an? (?:valid|whole|decimal)\b|\bmust be\b|\berror\b/i;
  const reddish = (n) => {
    const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(getComputedStyle(n).color || '');
    return !!m && +m[1] > 150 && +m[2] < 110 && +m[3] < 110;
  };
  const helperError = (els) => {
    for (const x of els || []) {
      const ids = (x.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
      for (const node of ids.map((id) => document.getElementById(id)).filter(Boolean)) {
        const parts = [node, ...node.querySelectorAll('*')].filter((n) =>
          !n.closest('[aria-live]') && Array.from(n.childNodes).some((c) => c.nodeType === 3 && c.textContent.trim()));
        const text = clean(parts.map((n) => Array.from(n.childNodes).filter((c) => c.nodeType === 3)
          .map((c) => c.textContent).join(' ')).join(' ').replace(COUNTER, ' '));
        if (text && (ERROR_WORDS.test(text) || parts.some(reddish))) return text;
      }
    }
    return '';
  };
  // A radio group's error in the 2026-10-14 layout: a plain line right after the group ("This field
  // is required"), outside the group and not linked to it.
  const siblingError = (box) => {
    if (!box || !box.matches('fieldset, [role="radiogroup"]')) return '';
    let n = box.nextElementSibling;
    for (let i = 0; n && i < 2; i++, n = n.nextElementSibling) {
      if (hasControls(n)) break;
      const t = clean(n.textContent);
      if (t && (ERROR_WORDS.test(t) || reddish(n))) return t;
    }
    return '';
  };
  const errorIn = (box, els) => {
    const e = box && hints.error ? box.querySelector(hints.error) : null;
    if (e && clean(e.textContent)) return clean(e.textContent);
    const bad = (els || []).find((x) => x.getAttribute('aria-invalid') === 'true');
    if (!bad) return helperError(els) || (box && box !== els[0] ? helperError([box]) : '') || siblingError(box);
    const ids = (bad.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
    return clean(ids.map((id) => document.getElementById(id)).filter(Boolean).map((n) => n.textContent).join(' '))
      || 'LinkedIn marked this answer as invalid';
  };
  const labelFor = (el) => {
    if (el.id) {
      const l = root.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && clean(l.textContent)) return textOf(l);
    }
    const wrap = el.closest('label');
    if (wrap && textOf(wrap)) return textOf(wrap);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map(textOf).join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const box = containerOf(el);
    const l = box && box.querySelector('label, legend');
    if (l && textOf(l)) return textOf(l);
    return clean(el.getAttribute('placeholder') || el.name || '');
  };
  const optionLabel = (el) => {
    if (el.id) {
      const l = root.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && textOf(l)) return textOf(l);
    }
    const wrap = el.closest('label');
    if (wrap && textOf(wrap)) return textOf(wrap);
    // 2026-10-14 layout: an empty <label for>, the option's text beside it inside its role="radio"
    // box (whose aria-label is the question, not the option).
    const optionBox = el.closest('[role="radio"], [role="checkbox"]');
    if (optionBox && optionBox.querySelectorAll('input').length === 1 && textOf(optionBox)) return textOf(optionBox);
    return clean(el.getAttribute('aria-label') || el.value || '');
  };
  // The question of a radio / checkbox group without legend or labelledby (2026-10-14 layout): the
  // options' shared aria-label (radio only), else the text just before the group.
  const questionBefore = (fs) => {
    let n = fs && fs.previousElementSibling;
    for (let i = 0; n && i < 2; i++, n = n.previousElementSibling) {
      if (hasControls(n)) break;
      if (clean(n.textContent)) return n;
    }
    return null;
  };
  const required = (els, box, raw) =>
    els.some((e) => e.required || e.getAttribute('aria-required') === 'true') ||
    !!(box && (box.matches('[aria-required="true"]') ||
      box.querySelector('[aria-required="true"], [required], [class*="required"]'))) ||
    /\*\s*$/.test(raw || '');
  window.__jaSeq = window.__jaSeq || 0;
  const newId = () => 'f' + (++window.__jaSeq);
  const fields = [];
  const groups = new Map();
  for (const el of root.querySelectorAll('input, select, textarea')) {
    if (inSkip(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['hidden', 'file', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    const box = containerOf(el);
    if (!visible(el) && !visible(box)) continue;
    if (type === 'radio' || type === 'checkbox') {
      const fs = el.closest('fieldset') || (type === 'radio' ? el.closest('[role="radiogroup"]') : null);
      const key = fs || (type === 'radio' && el.name ? 'name:' + el.name : el);
      let group = groups.get(key);
      if (!group) {
        const legend = fs && fs.querySelector('legend');
        let groupLabel = legend ? textOf(legend) : '';
        if (!groupLabel && fs && fs.getAttribute('aria-labelledby')) {
          groupLabel = clean(fs.getAttribute('aria-labelledby').split(/\s+/)
            .map((id) => document.getElementById(id)).filter(Boolean).map(textOf).join(' '));
        }
        if (!groupLabel && fs) groupLabel = clean(fs.getAttribute('aria-label'));
        const before = questionBefore(fs);
        if (!groupLabel && type === 'radio') {
          const optionBox = el.closest('[role="radio"]');
          groupLabel = clean(optionBox && optionBox.getAttribute('aria-label'));
        }
        if (!groupLabel && before) groupLabel = textOf(before);
        group = {
          id: newId(), type, label: groupLabel, options: [], value: '', inputs: [],
          box: fs || box, dom_id: el.id || '', checked: false, raw: before ? clean(before.textContent) : '',
        };
        groups.set(key, group);
        fields.push(group);
      }
      const opt = group.options.length;
      el.setAttribute('data-ja-field', group.id);
      el.setAttribute('data-ja-opt', String(opt));
      group.options.push(optionLabel(el));
      group.inputs.push(el);
      continue;
    }
    const id = newId();
    el.setAttribute('data-ja-field', id);
    let kind = 'text';
    const mode = (el.getAttribute('inputmode') || '').toLowerCase();
    if (tag === 'select') kind = 'select';
    else if (tag === 'textarea') kind = 'textarea';
    else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') kind = 'combobox';
    else if (type === 'number' || /numeric/i.test(el.id) || mode === 'numeric' || mode === 'decimal') kind = 'numeric';
    else if (type === 'date' || el.name === 'artdeco-date') kind = 'date';
    let options = [];
    let value = el.value || '';
    if (kind === 'select') {
      options = Array.from(el.options).map((o) => clean(o.textContent)).filter((t) => !isPlaceholder(t));
      const o = el.options[el.selectedIndex];
      value = o && !isPlaceholder(o.textContent) ? clean(o.textContent) : '';
    }
    fields.push({ id, type: kind, label: labelFor(el), options, value, inputs: [el], box, dom_id: el.id || '', checked: false });
  }
  return fields.map((f) => {
    let { label, options, value, checked, type } = f;
    if (type === 'radio' || type === 'checkbox') {
      if (!label) label = labelFor(f.inputs[0]);
      const on = f.inputs.map((e, i) => (e.checked ? options[i] : null)).filter((x) => x !== null);
      value = on.join('\n');
      if (type === 'checkbox' && f.inputs.length === 1) {
        checked = f.inputs[0].checked;
        if (!label) label = options[0];
        options = [];
        value = checked ? 'true' : 'false';
      }
    }
    const boxLabel = f.box && f.box.querySelector('label, legend');
    const rawLabel = f.raw || (boxLabel && clean(boxLabel.textContent)) || label;
    return {
      id: f.id, label, type, required: required(f.inputs, f.box, rawLabel), options, value,
      error: errorIn(f.box, f.inputs), dom_id: f.dom_id, checked,
    };
  });
}
"""

# One read of a modal form (see BrowserSession.form_state): the fields (FORM_FIELDS_JS), the visible
# buttons of the dialog around the form, each tagged with a data-ja-btn handle for CLICK_HANDLE_JS,
# the dialog's text, and the page's other open dialogs (a "Discard?" confirmation, a success note).
FORM_STATE_JS = (
    r"""
({ scope, hints }) => {
  const readFields = (
"""
    + FORM_FIELDS_JS
    + r"""
  );
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  const hasControls = (el) => el.querySelector('input, select, textarea, button');
  const parts = scope.split(',').map((s) => s.trim()).filter(Boolean);
  let root = null;
  for (const want of [hasControls, () => true]) {
    for (const part of parts) {
      root = Array.from(document.querySelectorAll(part)).find((el) => visible(el) && want(el)) || null;
      if (root) break;
    }
    if (root) break;
  }
  const dialogs = hints.dialog || 'dialog[open], [role="dialog"], [role="alertdialog"]';
  const box = root ? (root.closest(dialogs) || root) : null;
  window.__jaBtnSeq = window.__jaBtnSeq || 0;
  const buttonsIn = (el) => Array.from(el.querySelectorAll('button, [role="button"]')).filter(visible).map((b) => {
    let id = b.getAttribute('data-ja-btn');
    if (!id) {
      id = 'b' + (++window.__jaBtnSeq);
      b.setAttribute('data-ja-btn', id);
    }
    return {
      id, label: clean(b.getAttribute('aria-label')), text: clean(b.innerText || b.textContent),
      disabled: !!b.disabled,
    };
  });
  const textOf = (el, limit) => clean(el.innerText || el.textContent).slice(0, limit);
  const others = Array.from(document.querySelectorAll(dialogs + ', dialog[open]'))
    .filter((d) => visible(d) && !(box && (box === d || box.contains(d) || d.contains(box))));
  let progress = '';
  let header = '';
  const text = box ? textOf(box, 4000) : '';
  if (box) {
    const bar = box.querySelector('progress, [role="progressbar"]');
    if (bar) progress = String(bar.getAttribute('value') || bar.getAttribute('aria-valuenow') || '');
    const pages = text.match(/\b\d+\s*\/\s*\d+\s+pages?\b/i);
    if (pages) progress = (progress ? progress + ' ' : '') + pages[0];
    const h = hints.header ? box.querySelector(hints.header) : null;
    header = h ? clean(h.textContent) : '';
  }
  const watch = (hints.watch_texts || []).map((t) => t.toLowerCase());
  const pageText = watch.length && document.body ? clean(document.body.innerText).toLowerCase() : '';
  return {
    present: !!root,
    fields: root ? readFields({ scope, hints, root }) : [],
    buttons: box ? buttonsIn(box) : [],
    text,
    header,
    progress,
    dialogs: others.slice(0, 5).map((d) => ({ text: textOf(d, 1000), buttons: buttonsIn(d) })),
    page_hits: watch.filter((t) => pageText.includes(t)),
  };
}
"""
)

# Clicks the button a FORM_STATE_JS read tagged (data-ja-btn) from the page's own script: no
# actionability waits, which never settle on LinkedIn's busy pages.
CLICK_HANDLE_JS = r"""
(handle) => {
  const el = document.querySelector('[data-ja-btn="' + handle + '"]');
  if (!el || !el.isConnected || el.disabled) return false;
  try { el.scrollIntoView({ block: 'center' }); } catch (e) {}
  el.click();
  return true;
}
"""

# Clicks the first visible button / link whose aria-label or text matches a pattern, inside the
# elements matching a selector list (the elements themselves count) or the whole page.
CLICK_BUTTON_JS = r"""
({ name, within }) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  const pattern = new RegExp(name, 'i');
  const clickable = 'button, a, [role="button"]';
  const scopes = within
    ? within.split(',').map((s) => s.trim()).filter(Boolean).flatMap((s) => Array.from(document.querySelectorAll(s)))
    : [document.body];
  for (const scope of scopes) {
    const candidates = [scope, ...scope.querySelectorAll(clickable)].filter((el) => el.matches(clickable));
    for (const el of candidates) {
      if (el.disabled || !visible(el)) continue;
      const names = [el.getAttribute('aria-label'), el.innerText || el.textContent].map(clean).filter(Boolean);
      const hit = names.find((n) => pattern.test(n));
      if (!hit) continue;
      try { el.scrollIntoView({ block: 'center' }); } catch (e) {}
      el.click();
      return hit;
    }
  }
  return '';
}
"""

SCROLL_FIND_JS = r"""
(selector) => {
  const item = document.querySelector(selector);
  if (!item) return false;
  document.querySelectorAll('[data-ja-scroll]').forEach((n) => n.removeAttribute('data-ja-scroll'));
  let el = item.parentElement;
  while (el && el !== document.body) {
    const s = getComputedStyle(el);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 10) {
      el.setAttribute('data-ja-scroll', '1');
      return true;
    }
    el = el.parentElement;
  }
  return true;
}
"""

SCROLL_STEP_JS = r"""
() => {
  const el = document.querySelector('[data-ja-scroll]');
  if (!el) {
    window.scrollBy(0, window.innerHeight * 0.8);
    return window.innerHeight + window.scrollY >= document.body.scrollHeight - 5;
  }
  el.scrollBy(0, el.clientHeight * 0.8);
  return el.scrollTop + el.clientHeight >= el.scrollHeight - 5;
}
"""

# The normal (non-headless) user agent of the installed browser, detected once per process.
_user_agent: str | None = None
_user_agent_lock = threading.Lock()


def under_rosetta() -> bool:
    """Whether this process is an Intel build translated by Rosetta on an Apple-silicon Mac."""
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        found = libc.sysctlbyname(b"sysctl.proc_translated", ctypes.byref(value), ctypes.byref(size), None, 0)
    except Exception:  # noqa: BLE001 - no answer: assume native
        return False
    return found == 0 and value.value == 1


def _friendly_error(error: Exception, channel: str, profile_dir: Path | None) -> BrowserUnavailable:
    text = str(error)
    if "is not found" in text or "Executable doesn't exist" in text or "not installed" in text:
        if channel == "chrome":
            return BrowserUnavailable(
                "Google Chrome was not found. Install Google Chrome, or set BROWSER_CHANNEL=chromium "
                "and run `.venv/bin/python -m playwright install chromium`."
            )
        return BrowserUnavailable(
            "Playwright's Chromium is not installed. Run `.venv/bin/python -m playwright install "
            "chromium`, or set BROWSER_CHANNEL=chrome to use the installed Google Chrome."
        )
    if profile_dir is not None and ("ProcessSingleton" in text or "profile" in text.lower()):
        return BrowserUnavailable(
            f"The browser profile {profile_dir.name!r} is already open in another Chrome window. "
            "Close that window and try again."
        )
    first_line = text.strip().splitlines()[0] if text.strip() else type(error).__name__
    return BrowserUnavailable(f"The browser couldn't start: {first_line}")


def _reports_activity(cls):
    """Wrap every public method so the browser watchdog sees activity before and after it."""
    from app.browser.worker import beat

    def wrap(method):
        @functools.wraps(method)
        def inner(*args, **kwargs):
            beat()
            try:
                return method(*args, **kwargs)
            finally:
                beat()

        return inner

    for name, value in list(vars(cls).items()):
        if not name.startswith("_") and callable(value):
            setattr(cls, name, wrap(value))
    return cls


@_reports_activity
class PlaywrightSession:
    def __init__(self, context, owned_browser=None):
        self.last_click_problem = ""  # why the last click() found nothing to click
        self._context = context
        self._browser = owned_browser  # set for guest sessions, which own their browser
        self._closed = False
        self._captures: list[tuple[str, list[CapturedResponse]]] = []
        context.on("close", lambda _: self._mark_closed())
        self._page = context.pages[0] if context.pages else context.new_page()
        self._watch(self._page)
        context.on("page", self._watch)

    def _mark_closed(self) -> None:
        self._closed = True

    def _watch(self, page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        for pattern, bucket in self._captures:
            if pattern in response.url:
                try:
                    body = response.text()
                except Exception:  # body gone (redirect, navigation): ignore
                    continue
                bucket.append(CapturedResponse(url=response.url, status=response.status, body=body))

    def _live_page(self):
        pages = [page for page in self._context.pages if not page.is_closed()]
        if not pages:
            raise BrowserUnavailable("The browser window was closed.")
        if self._page not in pages:
            self._page = pages[-1]
        return self._page

    def goto(self, url, *, wait_for=None, capture=None, timeout=45.0) -> PageResult:
        page = self._live_page()
        bucket: list[CapturedResponse] = []
        if capture:
            self._captures.append((capture, bucket))
        deadline = time.monotonic() + timeout
        try:
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                status = response.status if response is not None else None
            except PlaywrightTimeoutError:
                # Slow page (throttled sites often are): read what has loaded rather than failing.
                if page.url in ("", "about:blank"):
                    raise
                logger.info("Slow page load, reading what has loaded: %s", url)
                status = None
            if wait_for or capture:
                while time.monotonic() < deadline:
                    if capture and bucket:
                        break
                    if wait_for and self._has(page, wait_for):
                        break
                    page.wait_for_timeout(POLL_SECONDS * 1000)
            # Let late rendering settle a little before reading the page.
            page.wait_for_timeout(1000)
            return PageResult(url=page.url, status=status, html=self._content(page), captured=list(bucket))
        finally:
            if capture:
                self._captures.remove((capture, bucket))

    @staticmethod
    def _has(page, selector: str) -> bool:
        try:
            return page.query_selector(selector) is not None
        except Exception:  # the page navigated while we looked (client-side redirect)
            return False

    @staticmethod
    def _content(page) -> str:
        for _ in range(3):
            try:
                return page.content()
            except Exception:  # still navigating: wait for it to settle
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    page.wait_for_timeout(1000)
        return page.content()

    def scroll(self) -> None:
        page = self._live_page()
        page.mouse.wheel(0, 4000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2500)

    def html(self) -> str:
        return self._content(self._live_page())

    def current_url(self) -> str:
        return self._live_page().url

    def cookies(self) -> list[dict]:
        return [dict(cookie) for cookie in self._context.cookies()]

    def add_cookies(self, cookies: list[dict]) -> list[str]:
        rejected = []
        for cookie in cookies:  # one by one, so one odd cookie doesn't drop the rest
            try:
                self._context.add_cookies([cookie])
            except Exception:
                rejected.append(cookie.get("name", "?"))
        return rejected

    def clear_cookies(self, domain: str) -> None:
        self._context.clear_cookies(domain=re.compile(rf"(^|\.){re.escape(domain)}$"))

    def cookie_value(self, name: str, domain: str) -> str | None:
        for cookie in self._context.cookies():
            if cookie.get("name") == name and cookie.get("domain", "").lstrip(".").endswith(domain):
                return cookie.get("value")
        return None

    def api_get(self, url, headers) -> ApiResponse:
        response = self._context.request.get(url, headers=headers, timeout=30_000, max_redirects=0)
        body = None
        if response.ok:
            try:
                body = response.json()
            except Exception:
                body = None
        return ApiResponse(status=response.status, url=response.url, json=body)

    def resolve_link(self, url) -> ApiResponse:
        response = self._context.request.get(url, timeout=20_000, max_redirects=5)
        try:
            text = response.text()[:200_000]
        except Exception:
            text = ""
        return ApiResponse(status=response.status, url=response.url, text=text)

    def screenshot(self, path) -> bool:
        try:
            self._live_page().screenshot(
                path=str(path), full_page=False, timeout=30_000, animations="disabled", caret="hide"
            )
            return True
        except Exception as error:
            # LinkedIn pages can keep Playwright "waiting for fonts" forever; ask Chrome directly.
            try:
                import base64

                page = self._live_page()
                cdp = self._context.new_cdp_session(page)
                data = cdp.send("Page.captureScreenshot", {"format": "png"})
                Path(path).write_bytes(base64.b64decode(data["data"]))
                cdp.detach()
                return True
            except Exception:
                logger.warning("Screenshot failed: %s", _first_line(error))
                return False

    def title(self) -> str:
        try:
            return self._live_page().title()
        except Exception:
            return ""

    def click(self, selector: str) -> bool:
        page = self._live_page()
        problems: list[str] = []
        for part in (s.strip() for s in selector.split(",")):
            if not part:
                continue
            locator = page.locator(part)
            try:
                count = locator.count()
            except Exception as error:
                problems.append(f"{part}: {_first_line(error)}")
                continue
            for index in range(count):
                element = locator.nth(index)
                try:
                    if not element.is_visible():
                        problems.append(f"{part}[{index}] hidden")
                        continue
                    try:
                        element.scroll_into_view_if_needed(timeout=3_000)
                    except Exception:
                        pass  # best effort: LinkedIn's pages never "settle"; click anyway
                    try:
                        element.click(timeout=8_000)
                    except Exception as error:
                        # Covered by another element, or a page that ignores automated pointer clicks:
                        # click it from the page's own script instead.
                        problems.append(f"{part}[{index}] pointer click: {_first_line(error)}")
                        element.evaluate("el => el.click()")
                    self.last_click_problem = "; ".join(problems)
                    return True
                except Exception as error:
                    problems.append(f"{part}[{index}]: {_first_line(error)}")
        self.last_click_problem = "; ".join(problems) or "no element matched"
        logger.warning("Nothing clickable for %s: %s", selector, self.last_click_problem[:500])
        return False

    def scroll_list(self, item_selector: str, steps: int = 8) -> bool:
        page = self._live_page()
        try:
            if not page.evaluate(SCROLL_FIND_JS, item_selector):
                return False
            for _ in range(steps):
                at_end = page.evaluate(SCROLL_STEP_JS)
                page.wait_for_timeout(random.uniform(500, 1100))
                if at_end:
                    break
            return True
        except Exception as error:
            logger.info("Scrolling the list failed: %s", _first_line(error))
            return False

    def click_role(self, role: str, name: str, *, within: str | None = None) -> bool:
        page = self._live_page()
        pattern = re.compile(name, re.I)
        scopes = [page] if not within else [page.locator(part.strip()) for part in within.split(",") if part.strip()]
        for scope in scopes:
            try:
                locator = scope.get_by_role(role, name=pattern)
                count = locator.count()
            except Exception:
                continue
            for index in range(count):
                element = locator.nth(index)
                try:
                    if not element.is_visible():
                        continue
                    try:
                        element.scroll_into_view_if_needed(timeout=3_000)
                    except Exception:
                        pass  # best effort: LinkedIn's pages never "settle"; click anyway
                    try:
                        element.click(timeout=8_000)
                    except Exception:
                        element.evaluate("el => el.click()")
                    return True
                except Exception as error:
                    logger.info("Clicking %s %r failed: %s", role, name, _first_line(error))
        return False

    def _evaluate(self, script: str, arg, *, timeout: float | None = None, tries: int = 2):
        """Run a page script (a JS function of one argument) with a time limit per try.

        ``page.evaluate`` has no time limit and can wait for minutes on a page whose main thread is
        busy; ``wait_for_function`` with a predicate that is always truthy runs the script once,
        and its timeout is enforced outside the page. Raises the last error after ``tries``."""
        wrapped = "(arg) => { const out = (" + script + ")(arg); return { v: out === undefined ? null : out }; }"
        timeout = EVAL_TIMEOUT_SECONDS if timeout is None else timeout
        error: Exception | None = None
        for attempt in range(1, tries + 1):
            page = self._live_page()
            try:
                handle = page.wait_for_function(wrapped, arg=arg, timeout=timeout * 1000, polling=100)
                try:
                    return handle.json_value()["v"]
                finally:
                    try:
                        handle.dispose()
                    except Exception:
                        pass
            except Exception as failure:  # timeout, navigation, script error
                error = failure
                logger.info("A page script failed (try %d of %d): %s", attempt, tries, _first_line(failure))
        assert error is not None
        raise error

    def form_state(self, scope: str, hints: dict) -> dict | None:
        try:
            state = self._evaluate(FORM_STATE_JS, {"scope": scope, "hints": hints})
        except Exception as error:
            logger.warning("Reading the form failed: %s", _first_line(error))
            return None
        return dict(state) if isinstance(state, dict) else None

    def click_handle(self, handle: str) -> bool:
        # One try: a click that timed out may still run once the page is free again.
        try:
            return bool(self._evaluate(CLICK_HANDLE_JS, handle, timeout=EVAL_TIMEOUT_SECONDS * 1.5, tries=1))
        except Exception as error:
            logger.info("Clicking %s failed: %s", handle, _first_line(error))
            return False

    def click_button(self, name: str, *, within: str | None = None) -> bool:
        try:
            clicked = self._evaluate(
                CLICK_BUTTON_JS, {"name": name, "within": within or ""}, timeout=EVAL_TIMEOUT_SECONDS * 1.5, tries=1
            )
        except Exception as error:
            logger.info("Clicking the button %r failed: %s", name, _first_line(error))
            return False
        return bool(clicked)

    def form_fields(self, scope: str, hints: dict) -> list[dict]:
        page = self._live_page()
        try:
            fields = page.evaluate(FORM_FIELDS_JS, {"scope": scope, "hints": hints})
        except Exception as error:
            logger.warning("Reading the form failed: %s", _first_line(error))
            return []
        return [dict(field) for field in fields or []]

    def fill_field(self, field: dict, value: str, hints: dict) -> bool:
        page = self._live_page()
        selector = f"[data-ja-field='{field['id']}']"
        kind = field.get("type", "text")
        try:
            if kind in ("text", "numeric", "textarea", "date"):
                element = page.locator(selector).first
                element.scroll_into_view_if_needed(timeout=5_000)
                element.fill(value, timeout=8_000)
                element.blur(timeout=5_000)
                return element.input_value(timeout=5_000).strip() == value.strip()
            if kind == "select":
                element = page.locator(selector).first
                index = element.evaluate(
                    "(el, v) => Array.from(el.options).findIndex((o) => o.textContent.replace(/\\s+/g, ' ').trim() === v)",
                    value,
                    timeout=8_000,
                )
                if index < 0:
                    return False
                element.select_option(index=index, timeout=8_000)
                chosen = element.evaluate("el => (el.options[el.selectedIndex] || {}).textContent || ''", timeout=8_000)
                return " ".join(chosen.split()) == value
            if kind == "radio":
                options = list(field.get("options") or [])
                if value not in options:
                    return False
                return self._set_checked(page, f"{selector}[data-ja-opt='{options.index(value)}']", True)
            if kind == "checkbox":
                options = list(field.get("options") or [])
                if not options:
                    return self._set_checked(page, selector, value.strip().lower() in ("true", "yes", "1"))
                wanted = {v.strip() for v in value.split("\n") if v.strip()}
                return all(
                    self._set_checked(page, f"{selector}[data-ja-opt='{index}']", option in wanted)
                    for index, option in enumerate(options)
                )
            if kind == "combobox":
                element = page.locator(selector).first
                element.scroll_into_view_if_needed(timeout=5_000)
                element.click(timeout=5_000)
                element.fill("", timeout=5_000)
                element.press_sequentially(value, delay=90, timeout=15_000)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    suggestions = page.locator(hints.get("suggestions", "[role='option']"))
                    if suggestions.count() and suggestions.first.is_visible():
                        suggestions.first.click(timeout=5_000)
                        page.wait_for_timeout(500)
                        return bool(element.input_value(timeout=5_000).strip())
                    page.wait_for_timeout(300)
                return False
        except Exception as error:
            logger.info("Filling %r (%s) failed: %s", field.get("label"), kind, _first_line(error))
            return False
        return False

    @staticmethod
    def _set_checked(page, selector: str, wanted: bool) -> bool:
        """Tick or untick a radio / checkbox, checking after each way of clicking it."""
        element = page.locator(selector).first
        if element.is_checked(timeout=5_000) == wanted:
            return True
        element_id = element.get_attribute("id", timeout=5_000)
        group = element.get_attribute("name", timeout=5_000)
        option_text = element.evaluate(
            "el => { const b = el.closest('[role=\"radio\"], [role=\"checkbox\"]');"
            " return b ? b.textContent.replace(/\\s+/g, ' ').trim() : ''; }"
        )

        def option_box():
            # 2026-10-14 layout (Applicantz): the option's role="radio" box takes the click and the
            # input only shows the state; its <label for> is empty.
            return element.evaluate(
                "el => { const b = el.closest('[role=\"radio\"], [role=\"checkbox\"]');"
                " if (!b || b.querySelectorAll('input').length !== 1) return false; b.click(); return true; }"
            )

        def label():
            if element_id:
                found = page.locator(f'label[for="{element_id}"]').first
                if found.count():
                    found.click(timeout=5_000)

        def forced():
            element.set_checked(wanted, force=True, timeout=5_000)

        def scripted():
            element.evaluate("el => el.click()")

        def checked() -> bool | None:
            # Ticking can make the page draw the option again (LinkedIn, 2026-10-14), with a new id and
            # without the data-ja-* tags: then it is found by its group name and option text.
            for found in (element, page.locator(f'[id="{element_id}"]').first if element_id else None):
                if found is not None and found.count():
                    return found.is_checked(timeout=5_000)
            if group and option_text:
                return page.evaluate(
                    """([name, text]) => {
                      const same = Array.from(document.getElementsByName(name)).find((x) => {
                        const b = x.closest('[role="radio"], [role="checkbox"]');
                        return b && b.textContent.replace(/\\s+/g, ' ').trim() === text;
                      });
                      return same ? same.checked : null;
                    }""",
                    [group, option_text],
                )
            return None

        for attempt in (option_box, label, forced, scripted):
            try:
                attempt()
            except Exception:
                pass
            try:
                if checked() == wanted:
                    return True
            except Exception:
                pass
        return False

    def close_extra_pages(self) -> int:
        page = self._live_page()
        closed = 0
        for other in list(self._context.pages):
            if other is not page and not other.is_closed():
                try:
                    other.close()
                    closed += 1
                except Exception:
                    logger.debug("Closing a tab failed", exc_info=True)
        return closed

    def type_text(self, selector: str, text: str) -> bool:
        page = self._live_page()
        for part in (s.strip() for s in selector.split(",")):
            if not part:
                continue
            try:
                element = page.locator(part).first
                if not element.count() or not element.is_visible():
                    continue
                element.scroll_into_view_if_needed(timeout=5_000)
                element.click(timeout=5_000)
                element.fill("", timeout=5_000)
                element.press_sequentially(text, delay=60, timeout=30_000)
                typed = element.evaluate("el => (el.isContentEditable ? el.innerText : el.value) || ''")
                return " ".join(str(typed).split()) == " ".join(text.split())
            except Exception as error:
                logger.info("Typing into %s failed: %s", part, _first_line(error))
        return False

    def is_closed(self) -> bool:
        if self._closed:
            return True
        try:
            # A round trip lets Playwright deliver pending "close" events.
            self._context.cookies()
        except Exception:
            return True
        # On macOS, closing the last window can leave Chrome running with no pages.
        return self._closed or not any(not page.is_closed() for page in self._context.pages)

    def wait(self, seconds: float) -> None:
        try:
            pages = [page for page in self._context.pages if not page.is_closed()]
            if pages:
                pages[0].wait_for_timeout(seconds * 1000)
                return
        except Exception:
            pass
        time.sleep(seconds)

    def close(self) -> None:
        if not self._closed:
            try:
                self._context.close()
            except Exception:
                logger.debug("Closing the browser context failed", exc_info=True)
            self._closed = True
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                logger.debug("Closing the browser failed", exc_info=True)
            self._browser = None


class PlaywrightDriver:
    """Starts Playwright on first use; ``close()`` closes every session and stops Playwright."""

    def __init__(self, channel: str = "chrome"):
        self.channel = channel
        self._playwright = None
        self._sessions: list[PlaywrightSession] = []

    def _start(self):
        if self._playwright is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as error:  # pragma: no cover - dependency missing
                raise BrowserUnavailable(
                    "Playwright is not installed. Run `.venv/bin/pip install -r requirements.txt`."
                ) from error
            self._playwright = sync_playwright().start()
        return self._playwright

    def _launch_options(self, headless: bool) -> dict:
        options = {
            "channel": self.channel,
            "headless": headless,
            "args": LAUNCH_ARGS,
            "ignore_default_args": IGNORE_DEFAULT_ARGS,
        }
        if self.channel == "chrome" and CHROME_APP.exists() and under_rosetta():
            del options["channel"]
            options["executable_path"] = str(NATIVE_CHROME)
        return options

    def _headless_user_agent(self) -> str:
        global _user_agent
        with _user_agent_lock:
            if _user_agent is None:
                browser = self._start().chromium.launch(**self._launch_options(True))
                try:
                    page = browser.new_page()
                    _user_agent = page.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
                finally:
                    browser.close()
            return _user_agent

    def open(self, *, profile_dir: Path | None, headless: bool) -> PlaywrightSession:
        try:
            playwright = self._start()
            context_options: dict = {"locale": LOCALE, "timezone_id": TIMEZONE}
            if headless:
                context_options["user_agent"] = self._headless_user_agent()
                context_options["viewport"] = VIEWPORT
                context_options["reduced_motion"] = "reduce"  # fewer animations for a busy page to draw
            else:
                context_options["no_viewport"] = True  # a normal, resizable window
            if profile_dir is not None:
                profile_dir.mkdir(parents=True, exist_ok=True)
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir), **self._launch_options(headless), **context_options
                )
                session = PlaywrightSession(context)
            else:
                browser = playwright.chromium.launch(**self._launch_options(headless))
                session = PlaywrightSession(browser.new_context(**context_options), owned_browser=browser)
        except BrowserUnavailable:
            raise
        except Exception as error:
            raise _friendly_error(error, self.channel, profile_dir) from error
        self._sessions.append(session)
        return session

    def close(self) -> None:
        for session in self._sessions:
            session.close()
        self._sessions.clear()
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                logger.debug("Stopping Playwright failed", exc_info=True)
            self._playwright = None


def _first_line(error: Exception) -> str:
    text = str(error).strip()
    return (text.splitlines()[0] if text else type(error).__name__)[:200]
