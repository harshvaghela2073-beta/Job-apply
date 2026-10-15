"""Everything about Naukri's pages that can break when the site changes, in one place.

Checked live on 2026-10-13 (logged out):
- The search page loads its results from ``/jobapi/v3/search`` (JSON). Each job has ``jobId``,
  ``title``, ``companyName``, ``jdURL``, ``minimumExperience``, ``experienceText``,
  ``placeholders`` (experience / salary / location labels), ``footerPlaceholderLabel``
  ("Just Now", "1 Day Ago"), ``createdDate`` (epoch ms), ``todaysJob``, ``companyApplyJob``
  (true = "Apply on company site") and ``applyRedirectUrl`` (the company-site link).
- ``sort=f`` sorts by date, newest first. ``k`` takes a comma-separated keyword list.
  ``wfhType=2`` is the remote filter. Page N is the same URL with ``-N`` after the path.
- Headless Chrome gets "Access Denied" unless its user agent says "Chrome" (see the driver).
- Logged out, every job page shows a plain "Apply" button, so the job page can only tell
  one-click from company-site jobs when logged in (needs a live check after login).
"""

BASE_URL = "https://www.naukri.com"
SEARCH_API = "/jobapi/v3/search"
RESULTS_PER_PAGE = 20
REMOTE_PARAM = ("wfhType", "2")
SORT_BY_DATE = ("sort", "f")

LOGIN_URL = "https://www.naukri.com/nlogin/login"
CONFIRM_URL = "https://www.naukri.com/mnjuser/homepage"
COOKIE_DOMAIN = "naukri.com"
# Cookies expected after a Naukri login (names not verified yet, so the page check decides).
AUTH_COOKIES = ("nauk_at", "nauk_rt", "nauk_sid")
# Chrome fast path: cookie names that suggest a login. Tracking / bot-check cookies (HOWTORT, J,
# _t_ds, ak_bmsc, bm_sv) are set for logged-out visitors too. Needs a live check after login: the
# import logs every Naukri cookie name it sees, to tune this.
CHROME_LOGIN_COOKIE_PREFIXES = ("nauk_",)
CHROME_LOGIN_COOKIES = ("NKWAP", "MYNAUKRI_SESSION")
LOGGED_IN_URL_MARKERS = ("/mnjuser/",)
LOGIN_WALL_MARKERS = ("/nlogin", "login.naukri.com", "/login?", "/registration")

# Logged-in check on CONFIRM_URL (needs a live check after login). Naukri doesn't always redirect
# logged-out visitors, so the page must show a member widget AND no Login/Register buttons.
LOGGED_IN_MARKERS = (
    ".nI-gNb-drawer",
    ".nI-gNb-drawer__icon",
    ".view-profile-wrapper",
    "a[href*='/mnjuser/profile']",
    ".info__heading",
    "[class*='profile-completion']",
)
LOGGED_OUT_MARKERS = (
    "#login_Layer",
    "#register_Layer",
    "a[class*='lg-rg__login']",
    "a[class*='lg-rg__register']",
    "a[title='Jobseeker Login']",
    "a[title='Jobseeker Register']",
)
LOGGED_OUT_TEXTS = ("login", "register")  # exact text of a header link or button
HEADER_LOADING = (".nI-gNb-shimmer__lg-rg-loader",)  # header not rendered yet

# Account name on the logged-in homepage / profile page (best effort; needs a live check).
ACCOUNT_NAME = (
    ".info__heading",
    "[class*='info__heading']",
    ".nI-gNb-info__name",
    ".nI-gNb-drawer__username",
    "[class*='userName']",
    "[class*='user-name']",
    ".name-wrapper .name",
)

# Search result cards, used only when the JSON response wasn't captured (markup as of 2024-2026).
CARD = (
    "div.srp-jobtuple-wrapper",
    "div.cust-job-tuple",
    "article.jobTuple",
    "div[class*='jobTuple']",
)
CARD_LINK = "a[href*='/job-listings-']"
CARD_TITLE = ("a.title", "a[href*='/job-listings-']")
CARD_COMPANY = ("a.comp-name", ".comp-name", "a.subTitle", "[class*='comp-name']")
CARD_EXPERIENCE = ("span.expwdth", ".exp-wrap span", "li.experience span", "[class*='exp']")
CARD_LOCATION = ("span.locWdth", ".loc-wrap span", "li.location span", "[class*='loc']")
CARD_POSTED = ("span.job-post-day", "[class*='job-post-day']", "[class*='post-day']", ".jobTupleFooter span.fleft")
CARD_DESCRIPTION = ("span.job-desc", ".job-desc", ".job-description")

# Apply buttons on a job page (logged in).
COMPANY_SITE_BUTTON = ("#company-site-button", "button[class*='company-site']")
APPLIED_MARKER = ("#already-applied", "[class*='already-applied']")
APPLY_BUTTON = ("#apply-button", "button[class*='apply-button']")
COMPANY_SITE_TEXT = "apply on company site"
COMPANY_SITE_TEXTS = (COMPANY_SITE_TEXT, "apply on company website", "apply on employer site")

# One-click applying (needs a live check: the first real applications confirm these).
APPLY_SUCCESS_TEXT = (
    "you have successfully applied",
    "successfully applied",
    "applied successfully",
    "application sent",
)
APPLY_SUCCESS_URL = ("/myapply/", "saveapply")
# The saveApply URL reports Naukri's answer per job: multiApplyResp={"<job id>":200}. Seen live on
# 2026-10-14: the click navigated there so fast that Playwright reported the click as failed.
SAVE_APPLY_RESPONSE_PARAM = "multiApplyResp"
# The job no longer takes applications (needs a live check: no saved page shows this yet).
APPLY_CLOSED_TEXT = (
    "no longer accepting applications",
    "no longer accepting responses",
    "this job has expired",
    "job has expired",
    "this job is no longer available",
    "job you are looking for is no longer available",
)
# The recruiter-questions drawer ("chatbot") that some jobs open instead of applying directly.
APPLY_QUESTIONNAIRE = ("[class*='chatbot_Drawer']", "[class*='chatbot_DrawerContentWrapper']")
APPLY_QUESTIONNAIRE_TEXT = (
    "recruiter's questions",
    "recruiter\u2019s questions",
    "kindly answer all",
    "answer the recruiter",
)
APPLY_QUESTION = (
    "[class*='chatbot'] li.botItem .botMsg",
    "[class*='chatbot'] [class*='botMsg']",
    "[class*='chatbot_ListItem'] span",
)
APPLY_QUOTA_TEXT = ("daily quota", "reached the daily limit", "maximum number of applies")

# Answering the drawer (app/sources/naukri/chatbot.py). From the saved pages of 2026-10-13/14 and
# the class names in the chatbot's own stylesheet on those pages. One question at a time: a bot
# bubble (li.botItem) asks, the answer goes in the footer, "Save" sends it and it shows up as a
# user bubble (li.userItem); a typing indicator (dots) runs before the next question. Only the
# text box and the Yes/No radio buttons were seen live; checkboxes and chips need a live check.
CHATBOT_ROOT = ("[class*='chatbot_Drawer']",)
CHATBOT_HIDDEN_CLASSES = ("chatbot_rightHide", "chatbot_leftHide", "chatbot_bottomHide", "chatbot_topHide")
CHATBOT_BOT_ITEM = "botItem"  # li classes in the message list
CHATBOT_USER_ITEM = "userItem"
CHATBOT_BOT_TEXT = (".botMsg", ".msg")
CHATBOT_TYPING = ("._chatBotdot", "[class*='typing']")
CHATBOT_RADIO_GROUP = (".singleselect-radiobutton-container",)
CHATBOT_RADIO_OPTION = (".ssrc__radio-btn-container",)
CHATBOT_CHECKBOX_GROUP = (".multicheckboxes-container",)
CHATBOT_CHIP = (".chatbot_Chip",)
CHATBOT_CHIP_DISABLED = "cursorDisabled"
CHATBOT_TEXT_INPUT = (
    "div.textArea[contenteditable='true']",
    "[class*='tac__textarea'][contenteditable='true']",
    "textarea[class*='tac__textarea']",
)
CHATBOT_DATE = (".dob__container",)
CHATBOT_DATE_PARTS = (".dob__input.day", ".dob__input.month", ".dob__input.year")
# Widgets the tool can't fill yet (search lists, multi-select layers, a calendar): left for you.
CHATBOT_UNSUPPORTED = {
    ".singleselect-sugg-container": "a searchable list",
    ".multiselect-suggestor-container": "a multi-select list",
    ".calendar-container": "a calendar",
}
CHATBOT_INLINE_ERROR = ".mcc__errorMsg, .msc__errorMsg, .chatbot_ddMulti_maxErr"
CHATBOT_SAVE = ".sendMsgbtn_container .send .sendMsg, .sendMsgbtn_container .sendMsg"
CHATBOT_SAVE_WRAPPER = ".sendMsgbtn_container .send"
CHATBOT_HIDDEN_ANCESTOR_CLASSES = ("d-none", "chatbot_Hide", "chatbot_Hidden", "ssc__chatbotHide", "visibility-hidden")
# Bot messages that aren't questions: the greeting and the closing thanks.
CHATBOT_NOT_A_QUESTION = APPLY_QUESTIONNAIRE_TEXT + (
    "thank you for showing interest",
    "thank you for your response",
    "thanks for your response",
    "thank you for answering",
)
# A bot message saying the last answer was rejected (needs a live check).
CHATBOT_ERROR = (
    r"\bplease (?:enter|select|provide|type) (?:a |an )?(?:valid|whole|correct|numeric)\b"
    r"|\binvalid (?:input|answer|value|number|response|format)\b"
    r"|\bis not (?:a )?valid\b|\bnot a valid\b|\bonly (?:numbers|digits|numeric values) (?:are )?allowed\b"
)

# Logged-in job page header, read by the auto-apply skip rules (needs a live check).
JOB_HEADER = (
    "section[class*='job-header-container']",
    "[class*='jhc__job-header-cont']",
    "[class*='job-header-container']",
    "[class*='jhc__left']",
)
JOB_COMPANY = ("[class*='jhc__comp-name'] a", "[class*='jhc__comp-name']", "a[href*='-jobs-careers-']")
JOB_SALARY = ("[class*='jhc__salary'] span", "[class*='jhc__salary']")
JOB_EXPERIENCE = ("[class*='jhc__exp'] span", "[class*='jhc__exp']")
JOB_KEY_SKILLS = ("[class*='key-skill'] a", "[class*='key-skill'] span a", "[class*='keySkill'] a")
JOB_DESCRIPTION = ("[class*='JDC__dang-inner-html']", "[class*='dang-inner-html']", "section[class*='job-desc']")

# Text that means Naukri blocked the request.
BLOCK_TEXT = ("access denied", "are you a robot", "unusual activity", "verify you are human", "captcha")
BLOCK_STATUSES = (403, 429)
