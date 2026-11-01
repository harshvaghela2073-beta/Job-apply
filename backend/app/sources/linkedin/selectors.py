"""Everything about LinkedIn's pages that can break when the site changes, in one place.

Since 2026-10-13 the job search uses the saved login (logged-out searches got HTTP 429 and a login
wall from the candidate's connection). The logged-in search, job page and Easy Apply selectors
below were written from LinkedIn's known markup and **need a live check**.

Public job search (no login; no longer used by the scanner), checked live on 2026-10-13:
- ``/jobs/search/?keywords=&location=&f_TPR=r<seconds>&f_E=<levels>`` returns up to 60 cards;
  ``/jobs-guest/jobs/api/seeMoreJobPostings/search?<same>&start=N`` returns 10 more each.
- ``f_TPR=r3600`` (any number of seconds) limits to jobs posted in that window. ``f_AL=true`` is
  Easy Apply only, ``f_WT=2`` remote. Keywords take boolean ``OR``. ``sortBy=DD`` isn't honoured
  by the public page, so every card's own date is checked.
- Card ``<time datetime>`` is a date in UTC; its text ("3 hours ago") is used first.

Logged-in pages (login detection, hiring-post search) could not be checked without a login:
they **need a live check after login**.
"""

BASE_URL = "https://www.linkedin.com"

# --- Public job search (guest pages; kept for reference, not used by the scanner) ---------------
JOB_SEARCH_URL = "https://www.linkedin.com/jobs/search/"
JOB_SEARCH_MORE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOBS_FIRST_PAGE = 60
JOBS_MORE_PAGE = 10
JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{id}/"
# Experience-level filter (f_E): 1 internship, 2 entry, 3 associate, 4 mid-senior, 5 director.
LEVEL_LABELS = {"2": "Entry level", "3": "Associate", "4": "Mid-Senior level"}
REMOTE_PARAM = ("f_WT", "2")
EASY_APPLY_PARAM = ("f_AL", "true")
REMOTE_SEARCH_LOCATION = "India"
# LinkedIn's names for cities; others are searched as "<City>, India".
CITY_SEARCH_NAMES = {"bengaluru": "Bengaluru, Karnataka, India"}

RESULT_COUNT = (".results-context-header__job-count",)
JOB_CARD = ("div.base-card", "div.base-search-card", "div.job-search-card")
JOB_CARD_LINK = ("a.base-card__full-link", "a[href*='/jobs/view/']")
JOB_CARD_TITLE = (".base-search-card__title",)
JOB_CARD_COMPANY = (".base-search-card__subtitle",)
JOB_CARD_LOCATION = (".job-search-card__location",)
JOB_CARD_TIME = ("time",)
JOB_CARD_BENEFIT = (".job-posting-benefits__text",)

# Public pages that mean "blocked": auth walls, sign-in pages, security checks.
GUEST_BLOCK_URL_MARKERS = ("/authwall", "/login", "/signup", "/checkpoint", "/uas/")
BLOCK_STATUSES = (403, 429, 999)

# --- Login (needs a live check after login) ------------------------------------------------------
LOGIN_URL = "https://www.linkedin.com/login"
CONFIRM_URL = "https://www.linkedin.com/feed/"
COOKIE_DOMAIN = "linkedin.com"
AUTH_COOKIES = ("li_at",)
# Authenticated check that doesn't depend on page markup (LinkedIn changes its class names often).
# The context's cookies are sent; csrf-token is the JSESSIONID cookie value with quotes stripped.
ME_API_URL = "https://www.linkedin.com/voyager/api/me"
ME_API_HEADERS = {
    "x-restli-protocol-version": "2.0.0",
    "accept": "application/vnd.linkedin.normalized+json+2.1",
}
CSRF_COOKIE = "JSESSIONID"
LOGGED_IN_URL_MARKERS = ("/feed",)
# Logged-in check on CONFIRM_URL: the signed-in navigation or profile, and no "Sign in" /
# "Join now" (needs a live check after login).
LOGGED_IN_MARKERS = (
    "img.global-nav__me-photo",
    ".global-nav__me",
    "#global-nav",
    "nav.global-nav",
    ".feed-identity-module",
    ".share-box-feed-entry__trigger",  # "Start a post"
    "a[href*='/messaging/']",  # member-only navigation
    "a[href*='/notifications/']",
    "a[href*='/mynetwork/']",
)
LOGGED_OUT_MARKERS = (
    "a.nav__button-secondary",
    "a.nav__button-primary",
    "form.sign-in-form",
    "a[data-tracking-control-name*='nav-header-signin']",
    "a[data-tracking-control-name*='nav-header-join']",
)
LOGGED_OUT_TEXTS = ("sign in", "join now")  # exact text of a link or button
LOGIN_WALL_MARKERS = ("/login", "/uas/", "/authwall", "/checkpoint", "/signup")
SECURITY_CHECK_MARKERS = ("/checkpoint", "/challenge")
# The account name: the "Me" photo's alt text, or the profile card on the feed.
ACCOUNT_NAME_IMAGES = ("img.global-nav__me-photo", "img.feed-identity-module__member-photo")
ACCOUNT_NAME_TEXT = (
    ".feed-identity-module__actor-meta a",
    ".profile-card-name",
    ".t-16.t-black.t-bold",
)

# --- Hiring-post search (needs a live check after login) -----------------------------------------
CONTENT_SEARCH_URL = "https://www.linkedin.com/search/results/content/"
CONTENT_SEARCH_FILTERS = (
    ("datePosted", '"past-24h"'),
    ("sortBy", '"date_posted"'),
    ("origin", "FACETED_SEARCH"),
)
# The Google Form searches look back a week (the candidate chose 7 days on 2026-10-15: forms stay open
# for days, and form posts for these roles are rare in one day).
CONTENT_SEARCH_PAST_WEEK = ("datePosted", '"past-week"')
FORM_POST_MAX_AGE_DAYS = 7
POST_SCROLLS = 1  # extra batches loaded by scrolling, each counted as a page load
POST_FORM_SCROLLS = 3  # more batches for the Google Form searches (2026-10-15)
# The home feed, read before the searches (2026-10-21): the first batch and FEED_SCROLLS more.
FEED_URL = "https://www.linkedin.com/feed/"
FEED_SCROLLS = 6
# Page loads per post scan (feed, searches and scrolls), instead of the shared SCAN_MAX_PAGE_LOADS
# (20): the feed 7 + 5 form searches x 4 + 8 role searches x 2 = 43.
POST_MAX_PAGE_LOADS = 44
# LinkedIn's post search as of 2026-10-14 ("SDUI"): generated class names, no data-urn. Each post is a
# div whose componentkey is "expanded<id>FeedType_FLAGSHIP_SEARCH"; its text is in the expandable text box.
# The home feed's markup isn't on a saved page yet (needs a live check): any componentkey with a
# "FeedType_" of any kind counts, with the same text box; then the older layouts' parsers.
SDUI_POST = "[componentkey*='FeedType_']"
SDUI_POST_TEXT = ("[data-testid='expandable-text-box']",)
# The "… more" button inside the text box (the whole text is already in the box).
SDUI_POST_MORE = ("[data-testid='expandable-text-button']", "button")
POST_WAIT = "[data-testid='expandable-text-box'], div.feed-shared-update-v2, [data-urn^='urn:li:activity']"
FEED_WAIT = POST_WAIT + ", [componentkey*='FeedType_']"
POST_CONTAINER = (
    "div.feed-shared-update-v2[data-urn]",
    "div[data-urn*='urn:li:activity:']",
    "[data-chameleon-result-urn*='urn:li:activity:']",
)
POST_TEXT = (
    ".update-components-text",
    ".feed-shared-inline-show-more-text",
    ".feed-shared-update-v2__description",
    ".feed-shared-text",
    "span.break-words",
)
POST_ACTOR_NAME = (
    ".update-components-actor__title span[aria-hidden='true']",
    ".update-components-actor__name span[aria-hidden='true']",
    ".update-components-actor__title",
    ".update-components-actor__name",
    ".feed-shared-actor__name",
)
# The author's profile link (``/in/<slug>``; a ``/company/`` link means a company page posted it).
POST_ACTOR_LINK = (
    "a.update-components-actor__meta-link",
    "a.update-components-actor__image",
    ".update-components-actor__container a[href]",
    ".update-components-actor a[href]",
    "a.feed-shared-actor__container-link",
    ".feed-shared-actor a[href]",
)
POST_ACTOR_HEADLINE = (
    ".update-components-actor__description span[aria-hidden='true']",
    ".update-components-actor__description",
    ".feed-shared-actor__description",
)
POST_TIME = (
    ".update-components-actor__sub-description span[aria-hidden='true']",
    ".update-components-actor__sub-description",
    ".feed-shared-actor__sub-description",
)
POST_URL = "https://www.linkedin.com/feed/update/{urn}/"
# Shortened links in posts; opened to find a Google Form behind them.
SHORT_LINK_HOSTS = ("lnkd.in",)
# Short links followed per run, with a plain request each (no page load; 2026-10-15: was 5 page loads,
# usually crowded out by the searches).
MAX_SHORT_LINKS = 15


# --- Logged-in job search (needs a live check) ---------------------------------------------------
# https://www.linkedin.com/jobs/search/?keywords=..&location=Bengaluru%2C%20Karnataka%2C%20India
#   &f_TPR=r86400&sortBy=DD&f_E=2%2C3%2C4  (+ f_AL=true: Easy Apply only; remote: location=India&f_WT=2)
# The saved results page (2026-10-14) is the older layout: li[data-occludable-job-id] > div.job-card-
# container[data-job-id], 7 cards rendered of 25, their footers still placeholders, no embedded JSON.
# Only the first page per location is rendered; the rest uses the captured list request (search_api.py).
SEARCH_TIME_WINDOW = ("f_TPR", "r86400")  # past 24 hours; each job's own date is still checked
SEARCH_SORT = ("sortBy", "DD")  # newest first
SEARCH_PAGE_SIZE = 25  # results per page; the next page is &start=25
# Voyager JSON embedded in the page (<code id="bpr-guid-...">) and the list's own API calls.
SEARCH_JSON_BLOCKS = "code"
SEARCH_CAPTURE = "voyagerJobsDashJobCards"
# Calling the captured list request again (search_api.py), with csrf-token = JSESSIONID (no quotes).
SEARCH_API_HEADERS = {
    "x-restli-protocol-version": "2.0.0",
    "accept": "application/vnd.linkedin.normalized+json+2.1",
    "x-li-lang": "en_US",
}
# A rendered card whose footer (date, "Easy Apply") hasn't rendered yet shows only placeholders
# (saved page, 2026-10-14): its apply type is unknown, not "company site".
SEARCH_CARD_GHOST = "job-card-container__ghost-placeholder"
# Result cards. The list lazy-renders: cards below the fold are empty <li> until scrolled to.
SEARCH_CARD = ("div.job-card-container[data-job-id]", "li[data-occludable-job-id]", "div[data-job-id]")
SEARCH_CARD_ID_ATTRS = ("data-job-id", "data-occludable-job-id")
SEARCH_CARD_LINK = ("a.job-card-list__title--link", "a.job-card-container__link", "a[href*='/jobs/view/']")
SEARCH_CARD_TITLE = (
    ".job-card-list__title--link strong",
    ".job-card-list__title strong",
    "a.job-card-container__link strong",
    ".artdeco-entity-lockup__title strong",
    ".job-card-list__title--link",
    ".artdeco-entity-lockup__title",
)
SEARCH_CARD_COMPANY = (
    ".artdeco-entity-lockup__subtitle",
    ".job-card-container__primary-description",
    ".job-card-container__company-name",
)
SEARCH_CARD_LOCATION = (
    ".artdeco-entity-lockup__caption li",
    ".artdeco-entity-lockup__caption",
    ".job-card-container__metadata-item",
)
SEARCH_CARD_FOOTER = (".job-card-container__footer-item", ".job-card-list__footer-wrapper li")
SEARCH_CARD_TIME = ("time",)
SEARCH_CARD_INSIGHT = (".job-card-container__job-insight-text", ".job-card-list__insight")
EASY_APPLY_TEXT = "easy apply"
# What goto() waits for: a card, or the "no results" banner (not the empty list skeleton).
SEARCH_READY = "li[data-occludable-job-id], div.job-card-container, .jobs-search-no-results-banner"
# Scrolled step by step so lazy cards render (the scrollable ancestor of the first card).
SEARCH_LIST_ITEM = "li[data-occludable-job-id], div.job-card-container"
SEARCH_SCROLL_STEPS = 8
SEARCH_RESULT_COUNT = (
    ".jobs-search-results-list__subtitle",
    "small.jobs-search-results-list__text",
    ".jobs-search-results-list__text",
)
SEARCH_NO_RESULTS = (".jobs-search-no-results-banner", ".jobs-search-two-pane__no-results-banner--expand")
SEARCH_NO_RESULTS_TEXT = ("no matching jobs found", "no jobs found", "no results found")

# --- Job page (logged in; needs a live check) ------------------------------------------------------
JOB_TITLE = (
    ".job-details-jobs-unified-top-card__job-title h1",
    ".job-details-jobs-unified-top-card__job-title",
    ".jobs-unified-top-card__job-title",
    "h1.t-24",
    "h1",
)
JOB_COMPANY = (
    ".job-details-jobs-unified-top-card__company-name a",
    ".job-details-jobs-unified-top-card__company-name",
    ".jobs-unified-top-card__company-name a",
    ".jobs-unified-top-card__company-name",
)
JOB_TOP_CARD = (
    ".job-details-jobs-unified-top-card__container--two-pane",
    ".job-details-jobs-unified-top-card__primary-description-container",
    ".jobs-unified-top-card",
    ".jobs-details__main-content",
)
# Pills under the title: salary, workplace type, job type, level ("Entry level", "Internship").
JOB_INSIGHTS = (
    ".job-details-fit-level-preferences button",
    ".job-details-jobs-unified-top-card__job-insight",
    ".jobs-unified-top-card__job-insight",
    ".job-details-preferences-and-skills",
)
JOB_DESCRIPTION = ("#job-details", ".jobs-description__content", ".jobs-description-content__text", ".jobs-box__html-content")
# "About the company": the industry line ("Staffing and Recruiting · 51-200 employees").
JOB_COMPANY_BOX = (".jobs-company__box", "section.jobs-company", ".jobs-company")
STAFFING_INDUSTRY = "staffing and recruiting"
JOB_LEVELS = ("internship", "entry level", "associate", "mid-senior level", "director", "executive")
# The apply button: "Easy Apply" (aria-label "Easy Apply to <title> at <company>") or an external
# "Apply" (aria-label "Apply to <title> on company website").
APPLY_BUTTON = (
    "button.jobs-apply-button",
    "a.jobs-apply-button",
    ".jobs-apply-button",
    # Job pages as of 2026-10-14: <button aria-label="Easy Apply to this job">, and for company-site
    # jobs an "Apply to <title> on company website" button or link.
    "button[aria-label*='Easy Apply']",
    "button[aria-label^='Apply to']",
    "a[aria-label^='Apply to']",
    # 2026-10-15 (saved pages): <a aria-label="Apply on company website" href="https://www.linkedin.com/
    # safety/go/?url=<the company's apply page>">Apply</a>.
    "a[aria-label$='on company website']",
    "button[aria-label$='on company website']",
)
# LinkedIn's "leaving LinkedIn" redirect around external links: the target is its ``url`` parameter.
SAFETY_REDIRECT = "linkedin.com/safety/go"
# The 2026-10-14 layout (saved pages): generated class names, the title in a <p>; found instead by
# the page <title> ("Java Developer | Acme | LinkedIn"), headings' text, and the pay pill
# ("1M INR/yr - 3M INR/yr") in the top part of the job details screen.
JOB_DETAILS_SCREEN = ("[data-sdui-screen*='JobDetails']",)
PAGE_TITLE_SUFFIX = "linkedin"
ABOUT_JOB_HEADING = "about the job"
ABOUT_COMPANY_HEADING = "about the company"
# Headings below the top card: the pay pill is looked for only above the first of these.
BELOW_TOP_CARD = (
    "use ai to assess how you fit", "people you can reach out to", "meet the hiring team", "about the job",
    "take the next step in your job search", "application status", "more jobs",
)
# "Applied 3 days ago" / "See application" after applying.
APPLIED_MARKER = (".artdeco-inline-feedback--success", ".jobs-s-apply__application-link", ".post-apply-timeline")
# 2026-10-14 layout (saved page): no Apply button, and a card "Application status / Application
# submitted 1 hour ago / Go to company site" (generated class names: found by its heading text).
APPLICATION_STATUS_HEADING = "application status"
APPLIED_PATTERN = (
    r"\bapplied\s+(?:\d+|an?|few)\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\b"
    r"|\bapplied\s+(?:just now|today|yesterday)\b|\bapplication submitted\b"
)
CLOSED_MARKER = (".jobs-details-top-card__apply-error", ".jobs-s-apply .artdeco-inline-feedback--error")
CLOSED_TEXT = ("no longer accepting applications",)
JOB_PAGE_READY = ", ".join(APPLY_BUTTON + APPLIED_MARKER + CLOSED_MARKER)

# --- Easy Apply form ---------------------------------------------------------------------------------
# The 2026-10-14 layout, from a saved page with the form open at step 1 of 5:
#   <dialog data-testid="dialog" aria-labelledby="dialog-header" open>
#     <button aria-label="Dismiss">  <header id="dialog-header"><h2>Apply to Acme</h2></header>
#     <div data-testid="dialog-content"><div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply">
#       <div id=P>20 percent complete</div><svg role="progressbar" aria-valuenow="20"> <p>1/5 pages</p>
#       <p>Contact info</p> ... <label for="_r_2d_"><div>Email address*</div></label>
#       <div><select id="_r_2d_" required>...</select></div> (Phone country code: the same; Mobile
#       phone number: <input type="tel" required>)
#       <footer><button type="button"><span><span>Next</span></span></button></footer>
# The footer buttons have no aria-label, only their text. The page ALSO has a carousel button with
# aria-label "Next" outside the dialog: buttons are only ever looked for inside the dialog.
# The "Save this application?" confirmation after Dismiss isn't on a saved page yet (needs a live
# check): any open dialog with a "Discard" button counts.
EASY_APPLY_BUTTON_NAME = r"easy apply"  # aria-label / text pattern of the job page's button
# Last resort, anywhere on the page: a button or link whose name starts with "Easy Apply".
EASY_APPLY_ANYWHERE_NAME = r"^\s*easy apply\b"
MODAL = (
    # The Easy Apply screen inside <dialog data-testid="dialog">. A bare [role='dialog'] is NOT
    # used: the search box's popover has that role too.
    "div[data-sdui-screen*='easyapply']",
    "dialog[data-testid='dialog']",
    "div.jobs-easy-apply-modal",
    "div[data-test-modal][role='dialog']",
)
MODAL_SCOPE = ", ".join(MODAL)
# Any open dialog (the Easy Apply one, or the confirmation / success one shown over it).
DIALOG_SCOPE = "dialog[open], [role='dialog'], [role='alertdialog'], .artdeco-modal"
# Footer and dialog buttons: aria-label (older layout) or text (2026-10-14 layout), lower case.
NEXT_LABEL = "Continue to next step"
REVIEW_LABEL = "Review your application"
SUBMIT_LABEL = "Submit application"
DISMISS_LABEL = "Dismiss"
BUTTON_KINDS = {
    "continue to next step": "next",
    "next": "next",
    "review your application": "review",
    "review": "review",
    "submit application": "submit",
    "submit": "submit",
    "done": "done",
    "dismiss": "dismiss",
    "discard": "discard",
    # "Job search safety reminder" shown before some forms.
    "continue applying": "continue",
}
SUCCESS_TEXT = ("application sent", "your application was sent", "application was sent to")
LIMIT_TEXT = ("easy apply limit", "limit daily submissions", "reached the daily application limit")
FOLLOW_COMPANY_ID = "follow-company-checkbox"
FOLLOW_COMPANY_LABEL = r"^follow\b.*\b(?:stay up to date|page)\b"
# Hints for the driver's form reader / filler (app.browser.playwright_driver.FORM_FIELDS_JS and
# FORM_STATE_JS).
FORM_HINTS = {
    # The dialog around the form: its buttons (Dismiss, Next, ...) and text are read.
    "dialog": "dialog[open], dialog[data-testid='dialog'], [role='dialog'], [role='alertdialog'], .artdeco-modal",
    # The step's heading in the older layout (the 2026-10-14 one shows "1/5 pages" instead).
    "header": "h3",
    # One question's wrapper (holds its label, control and inline error).
    "container": (
        "[data-test-form-element], .jobs-easy-apply-form-element, .fb-dash-form-element, "
        ".jobs-easy-apply-form-section__grouping"
    ),
    "error": ".artdeco-inline-feedback--error, [data-test-form-element-error-messages], [role='alert']",
    # Resume / cover-letter sections: never touched (the resume LinkedIn preselects is used).
    "skip": (
        ".jobs-document-upload-redesign-card__container, .jobs-document-upload, "
        "[class*='document-upload'], .jobs-resume-picker"
    ),
    # Typeahead suggestions (city).
    "suggestions": (
        "[role='listbox'] [role='option'], .basic-typeahead__selectable, .search-typeahead-v2__hit"
    ),
    "placeholder_options": "select an option",
}


# --- "Meet the hiring team" on the job page (logged in; needs a live check) ------------------------
# <div class="job-details-people-who-can-help__section--two-pane"><h2>Meet the hiring team</h2>
#   <div class="hirer-card__hirer-information"><a href="https://www.linkedin.com/in/<slug>/">
#     <span class="jobs-poster__name"><strong>Priya Sharma</strong></span></a>
#     <span class="hirer-card__connection-degree">· 2nd</span>
#     <div class="linked-area"><div class="text-body-small">Talent Acquisition at Acme</div></div>
#     <div class="hirer-card__job-poster">Job poster</div></div></div>
HIRING_TEAM_SECTION = (
    ".job-details-people-who-can-help__section--two-pane",
    ".job-details-people-who-can-help__section",
    ".jobs-poster",
)
HIRING_TEAM_CARD = (".hirer-card__hirer-information", ".jobs-poster__hirer-information")
HIRING_TEAM_HEADING = "meet the hiring team"  # fallback: the links under this heading
HIRING_TEAM_NAME = (".jobs-poster__name strong", ".jobs-poster__name", "strong", "span[aria-hidden='true']")
HIRING_TEAM_HEADLINE = (".linked-area .text-body-small", ".hirer-card__hirer-information .text-body-small", ".text-body-small")
HIRING_TEAM_DEGREE = (".hirer-card__connection-degree",)
HIRING_TEAM_POSTER = (".hirer-card__job-poster",)
JOB_POSTER_TEXT = "job poster"
PROFILE_LINK = "a[href*='/in/']"
HIRING_TEAM_READY = ", ".join(HIRING_TEAM_SECTION + HIRING_TEAM_CARD) + ", " + JOB_PAGE_READY
# Profile URLs are stored as https://www.linkedin.com/in/<slug>/ (lowercase slug).
PROFILE_URL = "https://www.linkedin.com/in/{slug}/"

# --- Connection note from a profile page (logged in; needs a live check) -----------------------
# Top card: "Connect" (aria-label "Invite <Name> to connect"), or "More" (aria-label "More actions")
# -> "Connect" in its menu. "Pending" when an invitation is waiting; "Message" only when connected.
PROFILE_READY = "main h1, .pv-top-card, h1.text-heading-xlarge"
PROFILE_TOP_CARD = (
    "main section.artdeco-card:first-of-type",
    "main .pv-top-card",
    "section.pv-top-card",
    "main section[data-member-id]",
)
PROFILE_TOP_CARD_SCOPE = ", ".join(PROFILE_TOP_CARD)
PROFILE_NAME = ("h1.text-heading-xlarge", ".pv-top-card h1", "main h1", "h1")
PROFILE_UNAVAILABLE_TEXT = ("this profile is not available", "this page doesn’t exist", "this page doesn't exist")
CONNECT_NAME = r"^\s*invite .+ to connect\s*$"  # accessible name of the Connect button
CONNECT_TEXT_NAME = r"^\s*connect\s*$"  # a Connect button without an aria-label
MORE_NAME = r"^\s*more(?: actions)?\s*$"
PENDING_NAME = r"^\s*pending\b"  # "Pending, click to withdraw invitation sent to <Name>"
MENU_ROLES = ("button", "menuitem")  # the "More" menu's items
# The 2026-10-15 layout: Connect is a link to the invitation page of that person (its vanityName).
CUSTOM_INVITE_LINK = "a[href*='/preload/custom-invite/']"
CUSTOM_INVITE_URL = "https://www.linkedin.com/preload/custom-invite/?vanityName={slug}"
NOT_INVITE_DIALOG_TESTIDS = ("popover-floating",)  # the search box's suggestions
# The invitation dialog: "Add a note to your invitation?" -> "Add a note" / "Send without a note".
INVITE_DIALOG = ("div[role='dialog']", "[role='alertdialog']", ".artdeco-modal", ".send-invite", "dialog[open]")
INVITE_DIALOG_SCOPE = ", ".join(INVITE_DIALOG)
ADD_NOTE_NAME = r"^\s*add a note\s*$"
SEND_NAME = r"^\s*(?:send invitation|send now|send)\s*$"  # never "Send without a note"
DISMISS_NAME = r"^\s*(?:dismiss|cancel|close)\s*$"
NOTE_TEXTAREA_IDS = ("custom-message",)  # <textarea name="message" id="custom-message" maxlength="300">
NOTE_HINTS = {"container": ".artdeco-modal__content, .send-invite__custom-message", "error": "[role='alert']"}
INVITE_SENT_TEXT = ("invitation sent", "your invitation to")  # toast: "Your invitation to Priya was sent."
WEEKLY_LIMIT_TEXT = ("weekly invitation limit", "reached the weekly limit", "weekly limit for invitations")
# Free accounts get a few personalized notes a month; then "Add a note" shows a Premium offer.
NOTE_LIMIT_TEXT = (
    "used all your personalized",
    "personalized invitations left",
    "monthly personalized invitations",
    "unlimited personalized invit",
    "out of personalized invitations",
)
EMAIL_REQUIRED_TEXT = ("enter their email", "email address to connect", "to verify this member knows you")
INVITE_ERROR_TEXT = ("invitation not sent", "unable to send", "couldn't send", "something went wrong")  # in the dialog or a toast only
# Where LinkedIn says what happened after Send: its toasts and alerts. The page itself carries hidden
# translations such as "Sorry, something went wrong" (2026-10-27), so it is never searched for errors.
INVITE_TOAST = (".artdeco-toast-item", "[data-test-artdeco-toast-item-type]", "[role='alert']", "[role='status']")
