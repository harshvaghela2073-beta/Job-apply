"""The driver's in-page scripts (FORM_FIELDS_JS / FORM_STATE_JS / CLICK_*_JS) on synthetic pages that
mirror LinkedIn's 2026-10-14 Easy Apply layout, in a local headless Chromium (``page.set_content``,
no network, no profile). Skipped when no Chromium can start. People and data are made up."""

import time

from app.browser import playwright_driver
from app.sources.linkedin import selectors as S
from app.sources.linkedin.easy_apply import read_state
from tests.headless import chromium, page_session  # noqa: F401 - fixtures

# The structure of a saved job page with the Easy Apply form open at step 1 of 5: a <dialog> with a
# "Dismiss" button, the Easy Apply screen, <label for=ID> + control pairs with generated ids, a
# footer button with only its text. Outside the dialog: a carousel button with aria-label "Next"
# and the search box's [role=dialog] popover (which must never count as the form).
EASY_APPLY_PAGE = """<html><body>
<script>window.clicked = [];</script>
<header><input aria-label="Search" role="combobox">
 <div role="dialog" aria-label="Search suggestions"><input type="text" aria-label="Search by title"></div></header>
<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
 <h1>Backend Developer</h1>
 <button type="button" aria-label="Easy Apply to this job" onclick="window.clicked.push('easy-apply')"><span>Easy Apply</span></button>
 <button type="button" aria-label="Next" onclick="window.clicked.push('carousel-next')">›</button>
</div>
<dialog data-testid="dialog" aria-labelledby="dialog-header" open>
 <button type="button" aria-label="Dismiss" onclick="window.clicked.push('dismiss')"><span>×</span></button>
 <div><header id="dialog-header"><h2>Apply to Acme Payments</h2></header>
 <div data-testid="dialog-content">
  <div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply">
   <div><div componentkey="5f0c-a1"><div><div>
    <div id="p1">20 percent complete</div>
    <div aria-labelledby="p1"><svg role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="20"></svg></div>
    <p>1/5 pages</p></div>
    <div><div data-testid="lazy-column"><div>
     <p>Contact info</p>
     <div><p><span>Asha Example</span></p><p>Bengaluru, Karnataka, India</p></div>
     <div><label for="_r_2d_"><div>Email address*</div></label>
      <div><select id="_r_2d_" required>
       <option value="" disabled hidden></option><option value="asha@example.com" selected>asha@example.com</option>
      </select></div></div>
     <div><div><label for="_r_2f_"><div>Phone country code*</div></label>
      <div><select id="_r_2f_" required>
       <option value="" disabled hidden></option><option value="ad">Andorra (+376)</option>
       <option value="in" selected>India (+91)</option><option value="us">United States (+1)</option>
      </select></div></div>
      <div componentkey="easyApplyFieldFocus_ea.q::1::PHONE_MOBILE::phoneNumber.valid">
       <label for="_r_2h_"><div>Mobile phone number*</div></label>
       <div><input required id="_r_2h_" aria-describedby="_r_2h_-info" type="tel" value="9000000001"></div>
       <div id="_r_2h_-info"></div></div></div>
    </div></div></div>
    <div><div><hr role="presentation"><footer>
     <button type="button" componentkey="0058-ab" onclick="window.clicked.push('footer-next')"><span><span>Next</span></span></button>
    </footer></div></div>
   </div></div>
  </div></div></div>
</dialog>
</body></html>"""

# "Save this application?" as its own <dialog>, over the form.
DISCARD_CONFIRM = """<dialog data-testid="dialog" open id="confirm">
 <h2>Save this application?</h2><p>Save to return to it later.</p>
 <button type="button" onclick="window.clicked.push('discard')"><span>Discard</span></button>
 <button type="button" onclick="window.clicked.push('save')"><span>Save</span></button>
</dialog>"""

# A later step: a radio question in a radiogroup labelled by its heading, and a text question.
QUESTIONS_PAGE = """<html><body><script>window.clicked = [];</script>
<dialog data-testid="dialog" open>
 <button type="button" aria-label="Dismiss"></button>
 <div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply">
  <p>3/5 pages</p>
  <div><span id="q1">Are you comfortable commuting to this job's location?*</span>
   <div role="radiogroup" aria-labelledby="q1" aria-required="true">
    <div><input type="radio" id="r1" name="commute" value="Yes"><label for="r1">Yes</label></div>
    <div><input type="radio" id="r2" name="commute" value="No"><label for="r2">No</label></div>
   </div></div>
  <div><label for="_r_3a_">Notice period (in days)*</label>
   <div><input id="_r_3a_" type="text" required value=""></div>
   <div role="alert">Enter a whole number between 0 and 90</div></div>
  <footer><button type="button" onclick="window.clicked.push('review')">Review</button></footer>
 </div>
</dialog></body></html>"""


def state(session, **extra_hints):
    return session.form_state(S.MODAL_SCOPE, {**S.FORM_HINTS, **extra_hints})


def test_form_state_reads_the_new_layout(page_session):
    page_session.show(EASY_APPLY_PAGE)
    raw = state(page_session)
    assert raw["present"] is True
    fields = {f["label"]: f for f in raw["fields"]}
    assert list(fields) == ["Email address", "Phone country code", "Mobile phone number"]
    email, country, phone = fields.values()
    assert (email["type"], email["required"], email["value"], email["options"]) == (
        "select", True, "asha@example.com", ["asha@example.com"]
    )
    assert (country["type"], country["value"]) == ("select", "India (+91)") and "Andorra (+376)" in country["options"]
    assert (phone["type"], phone["required"], phone["value"], phone["error"]) == ("text", True, "9000000001", "")
    # Only the dialog's own buttons: Dismiss (aria-label) and the footer's text-only "Next"; the
    # page's carousel "Next" and "Easy Apply" are outside the dialog.
    assert [(b["label"], b["text"]) for b in raw["buttons"]] == [("Dismiss", "×"), ("", "Next")]
    assert raw["progress"] == "20 1/5 pages"
    assert "Contact info" in raw["text"]

    view = read_state(raw)
    assert set(view.buttons) == {"dismiss", "next"} and view.discard is None
    assert [f.label for f in view.fields] == ["Email address", "Phone country code", "Mobile phone number"]


def test_click_by_handle_is_scoped_to_the_dialog(page_session):
    page_session.show(EASY_APPLY_PAGE)
    view = read_state(state(page_session))
    assert page_session.click_handle(view.buttons["next"])
    assert page_session.page.evaluate("window.clicked") == ["footer-next"]  # not the carousel's "Next"
    assert not page_session.click_handle("b-does-not-exist")


def test_click_button_finds_easy_apply(page_session):
    page_session.show(EASY_APPLY_PAGE)
    assert page_session.click_button(S.EASY_APPLY_BUTTON_NAME, within=", ".join(S.APPLY_BUTTON))
    assert page_session.page.evaluate("window.clicked") == ["easy-apply"]
    assert not page_session.click_button(r"^withdraw$")


def test_discard_confirmation_dialog(page_session):
    page_session.show(EASY_APPLY_PAGE.replace("</body>", DISCARD_CONFIRM + "</body>"))
    view = read_state(state(page_session))
    assert view.present and view.discard
    assert page_session.click_handle(view.discard)
    assert page_session.page.evaluate("window.clicked") == ["discard"]


def test_radio_group_and_inline_error(page_session):
    page_session.show(QUESTIONS_PAGE)
    raw = state(page_session)
    commute, notice = raw["fields"]
    assert (commute["label"], commute["type"], commute["options"], commute["required"]) == (
        "Are you comfortable commuting to this job's location?", "radio", ["Yes", "No"], True
    )
    assert (notice["label"], notice["error"]) == ("Notice period (in days)", "Enter a whole number between 0 and 90")
    assert raw["progress"] == "3/5 pages"
    assert page_session.fill_field(commute, "Yes", S.FORM_HINTS)
    assert page_session.fill_field(notice, "30", S.FORM_HINTS)
    again = {f["label"]: f["value"] for f in state(page_session)["fields"]}
    assert again == {"Are you comfortable commuting to this job's location?": "Yes", "Notice period (in days)": "30"}
    view = read_state(state(page_session))
    assert page_session.click_handle(view.buttons["review"])


def test_success_text_is_watched_on_the_whole_page(page_session):
    page_session.show("<html><body><div role='status'>Your application was sent to Acme Payments</div></body></html>")
    raw = state(page_session, watch_texts=list(S.SUCCESS_TEXT))
    assert raw["present"] is False and "your application was sent" in raw["page_hits"]
    assert read_state(raw).success


def test_a_busy_page_doesnt_block(page_session, monkeypatch):
    """A page whose main thread is busy: the read gives up after its time limit (and one retry)
    instead of waiting for the page."""
    monkeypatch.setattr(playwright_driver, "EVAL_TIMEOUT_SECONDS", 0.5)
    page_session.show(EASY_APPLY_PAGE)
    page_session.page.evaluate("() => { setTimeout(() => { const end = Date.now() + 5000; while (Date.now() < end) {} }, 0); }")
    page_session.page.wait_for_timeout(100)
    started = time.monotonic()
    assert page_session.form_state(S.MODAL_SCOPE, S.FORM_HINTS) is None
    assert not page_session.click_handle("b1")
    assert time.monotonic() - started < 4.5
    assert read_state(None).responded is False


# A rejected answer in the 2026-10-14 layout (TCS, step 4): the helper the input points to holds the
# red "Invalid input" line and the character counter; an unrejected box has only the counter.
REJECTED_PAGE = """<html><body><dialog data-testid="dialog" open>
 <div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply"><p>4/5 pages</p>
  <div><label for="_r_27_">Current ctc*</label>
   <div><input id="_r_27_" aria-describedby="_r_27_-info" type="text" required value="12 LPA"></div>
   <div data-testid="text-input-helper-text" id="_r_27_-info">
    <p style="color: rgb(203, 17, 45)">Invalid input</p>
    <div><p aria-live="polite"><span>6/20</span><span>6 of 20 characters</span></p></div></div></div>
  <div><label for="_r_29_">How many years of experience do you have in Java?*</label>
   <div><input id="_r_29_" aria-describedby="_r_29_-info" type="text" required value="1.9"></div>
   <div data-testid="text-input-helper-text" id="_r_29_-info">
    <div><p aria-live="polite"><span>3/20</span><span>3 of 20 characters</span></p></div></div></div>
  <div><label for="_r_31_">Website</label>
   <div><input id="_r_31_" aria-describedby="_r_31_-info" type="text" value=""></div>
   <div id="_r_31_-info"><p style="color: rgb(90, 90, 90)">Your portfolio or blog</p></div></div>
  <div><label for="_r_33_">Expected CTC*</label>
   <div><input id="_r_33_" aria-describedby="_r_33_-info" type="text" required value=""></div>
   <div id="_r_33_-info"><p style="color: rgb(90, 90, 90)">Please enter it in lakhs, a valid number</p></div></div>
  <footer><button type="button">Next</button></footer>
 </div></dialog></body></html>"""


def test_rejected_answer_in_the_helper_text(page_session):
    page_session.show(REJECTED_PAGE)
    errors = {f["label"]: f["error"] for f in state(page_session)["fields"]}
    assert errors == {
        "Current ctc": "Invalid input",
        "How many years of experience do you have in Java?": "",
        "Website": "",  # a grey hint is not an error
        "Expected CTC": "",
    }


# A Yes / No question in the 2026-10-14 layout (Applicantz): the question in a <p> before the
# radiogroup, each option a role="radio" box whose aria-label repeats the question, an empty
# <label for>, the option text beside it, and "This field is required" after the group.
RADIO_PAGE = """<html><body><dialog data-testid="dialog" open>
 <div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply"><p>4/4 pages</p>
  <div componentkey="easyApplyFieldFocus_ea_validation_p3_0_1">
   <p>Can you join within 15-30 days?*</p>
   <fieldset role="radiogroup" aria-describedby="error-message-_r_26_">
    <div><div role="radio" aria-label="Can you join within 15-30 days?">
     <div><div><input id="_r_27_" type="radio" name="radio-group-_r_26_"><label for="_r_27_"></label></div>
     <div><p>Yes</p></div></div></div>
    <div role="radio" aria-label="Can you join within 15-30 days?">
     <div><div><input id="_r_28_" type="radio" name="radio-group-_r_26_"><label for="_r_28_"></label></div>
     <div><p>No</p></div></div></div></div>
   </fieldset>
   <p style="color: rgb(203, 17, 45)">This field is required</p>
  </div>
  <div componentkey="easyApplyFieldFocus_ea_validation_p3_0_2">
   <p>Are you open to night shifts?</p>
   <fieldset role="radiogroup">
    <div role="radio" aria-label="Are you open to night shifts?"><input id="_r_31_" type="radio" name="radio-group-_r_30_">
     <label for="_r_31_"></label><p>Yes</p></div>
    <div role="radio" aria-label="Are you open to night shifts?"><input id="_r_32_" type="radio" name="radio-group-_r_30_">
     <label for="_r_32_"></label><p>No</p></div>
   </fieldset>
  </div>
  <footer><button type="button">Review</button></footer>
 </div></dialog></body></html>"""


def test_radio_question_in_the_new_layout(page_session):
    page_session.show(RADIO_PAGE)
    join, shifts = state(page_session)["fields"]
    assert (join["label"], join["type"], join["options"], join["required"], join["error"]) == (
        "Can you join within 15-30 days?", "radio", ["Yes", "No"], True, "This field is required"
    )
    assert (shifts["label"], shifts["options"], shifts["required"], shifts["error"]) == (
        "Are you open to night shifts?", ["Yes", "No"], False, ""
    )
    assert page_session.fill_field(join, "Yes", S.FORM_HINTS)
    assert state(page_session)["fields"][0]["value"] == "Yes"


# The option box takes the click; a click on the input itself (or its empty label) is undone, like
# LinkedIn's controlled inputs.
CONTROLLED_RADIO = """<html><body><dialog data-testid="dialog" open>
 <div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply">
  <div><p>Can you join within 15-30 days?*</p>
   <fieldset role="radiogroup">
    <div role="radio" aria-label="Can you join within 15-30 days?"
         onclick="if (event.target === this || !event.target.matches('input, label')) this.querySelector('input').checked = true">
     <input id="_r_27_" type="radio" name="g" onclick="event.preventDefault()"><label for="_r_27_"></label><p>Yes</p></div>
    <div role="radio" aria-label="Can you join within 15-30 days?"
         onclick="if (event.target === this || !event.target.matches('input, label')) this.querySelector('input').checked = true">
     <input id="_r_28_" type="radio" name="g" onclick="event.preventDefault()"><label for="_r_28_"></label><p>No</p></div>
   </fieldset></div>
  <footer><button type="button">Review</button></footer>
 </div></dialog></body></html>"""


# Ticking draws the option again (a new input with a new id), like LinkedIn did live.
REDRAWN_RADIO = CONTROLLED_RADIO.replace(
    "this.querySelector('input').checked = true",
    "(() => { const i = this.querySelector('input'); const n = i.cloneNode(); n.removeAttribute('data-ja-field');"
    " n.removeAttribute('data-ja-opt'); n.id = i.id + 'b'; i.replaceWith(n); n.checked = true; })()",
)


def test_a_radio_drawn_again_after_ticking_counts_as_ticked(page_session):
    page_session.show(REDRAWN_RADIO)
    join = state(page_session)["fields"][0]
    started = time.monotonic()
    assert page_session.fill_field(join, "Yes", S.FORM_HINTS)
    assert time.monotonic() - started < 5
    assert state(page_session)["fields"][0]["value"] == "Yes"


def test_a_controlled_radio_is_ticked_through_its_option_box(page_session):
    page_session.show(CONTROLLED_RADIO)
    join = state(page_session)["fields"][0]
    assert (join["label"], join["options"]) == ("Can you join within 15-30 days?", ["Yes", "No"])
    assert page_session.fill_field(join, "Yes", S.FORM_HINTS)
    assert state(page_session)["fields"][0]["value"] == "Yes"
