# API Contract (Phase 1)

The agreement between `backend/` and `frontend/`. Change this file first, then both sides.

- Base URL: configurable.
  - Backend dev default: `http://localhost:8000`.
  - The frontend reads it from `VITE_API_BASE_URL`.
- All endpoints are under `/api`. JSON only.
- Datetimes are ISO 8601 strings in UTC, e.g. `"2026-10-13T06:45:00Z"`. The frontend shows them in local time.
- List endpoints return `{ "items": [...], "total": <int> }` and accept `limit` (default 50, max 200;
  a higher value returns 422) and `offset` (default 0).
- Errors use FastAPI's default shape: `{ "detail": "message" }` with a 4xx/5xx status.

## Enums

| Enum | Values |
|---|---|
| `Platform` | `linkedin`, `naukri` |
| `ApplyType` | `easy_apply` (LinkedIn), `one_click` (Naukri), `company_site`, `google_form`, `email` |
| `JobStatus` | `found`, `queued`, `applied`, `needs_manual`, `failed`, `skipped` |
| `OutreachKind` | `email`, `connection_note`, `inmail` |
| `OutreachStatus` | `draft`, `approved`, `sent`, `rejected`, `failed` |
| `TemplateKind` | `answer` (application question), `linkedin_message` (connection note / InMail), `email` |

## Data models

### Job

```json
{
  "id": 12,
  "platform": "linkedin",
  "external_id": "3901234567",
  "title": "Senior Backend Engineer",
  "company": "Acme",
  "location": "Bengaluru (Hybrid)",
  "url": "https://www.linkedin.com/jobs/view/3901234567",
  "apply_url": "https://careers.acme.com/jobs/123",
  "form_url": null,
  "contact_email": null,
  "post_url": null,
  "short_description": "Python/FastAPI, 4-7 yrs, payments domain.",
  "apply_type": "company_site",
  "status": "needs_manual",
  "status_reason": null,
  "found_at": "2026-10-13T05:00:00Z",
  "applied_at": null,
  "updated_at": "2026-10-13T05:00:00Z",
  "posted_at": "2026-10-13T03:00:00Z",
  "experience": "1-4 Yrs",
  "matched_role": "Backend Developer",
  "priority": 140
}
```

- `(platform, external_id)` is unique. It is what stops the tool from applying to the same job twice.
- `apply_url` holds the company-website application link.
- `form_url` holds the Google Form link.
- `contact_email` holds the hiring person's email.
- `post_url` holds the LinkedIn post the job came from, when it came from a post.
- `status_reason` gives a human-readable reason for `failed`, `needs_manual` or `skipped`.
- These fields are filled by the scanner and can be `null` for older rows:
  - `posted_at`: when the platform says the job or post was published, estimated from labels such
    as "3 hours ago".
  - `experience`: the experience range as the platform shows it.
  - `matched_role`: the role from `search_roles` that the title matched.
  - `priority`: a sort key where lower means more important. It is
    `experience_bucket * 100 + role_index * 10 + location_index`, using the order in the
    `search_*` profile fields:
    - `experience_bucket` is 0 when the minimum experience is 1 year or less, 1 when it is 2 years,
      and 2 when it is 3 years.
    - `role_index` is the position of `matched_role` in the roles list.
    - `location_index` is the position of the location in the locations list.

### OutreachMessage

This is one approval queue for hiring-manager emails, connection notes and InMails.

```json
{
  "id": 3,
  "kind": "email",
  "job_id": 12,
  "recipient_name": "Priya Sharma",
  "recipient_profile_url": "https://www.linkedin.com/in/priya-sharma",
  "recipient_email": "priya@acme.com",
  "subject": "Application: Senior Backend Engineer",
  "body": "Hi Priya, ...",
  "status": "draft",
  "error": null,
  "created_at": "2026-10-13T05:00:00Z",
  "approved_at": null,
  "sent_at": null
}
```

- `subject` and `recipient_email` are used only when `kind` is `email`.
- Connection-note bodies are at most 300 characters. Return 422 when this is exceeded on create or update.

### UnansweredQuestion

A form question the tool met but couldn't answer from the Template Library.

```json
{
  "id": 7,
  "question": "Are you open to working from Pune office 3 days a week?",
  "platform": "naukri",
  "job_id": 15,
  "seen_at": "2026-10-13T05:00:00Z",
  "answered_at": null
}
```

### Template

A reusable piece of text, stored in the database: an answer to an application question, a
LinkedIn message, or an email.

```json
{
  "id": 4,
  "kind": "answer",
  "question": "What tech stack are you most familiar with?",
  "answer": "I have worked with Java, Golang, and Python. ...",
  "subject": null,
  "is_answered": true,
  "created_at": "2026-10-13T08:00:00Z",
  "updated_at": "2026-10-13T08:00:00Z"
}
```

- `question` is the application question for `answer` templates, and a short name for messages
  and emails (for example "Connection request: openings at [Company]").
- `answer` is the answer text or message body. Line breaks are kept.
- `subject` is used by `email` templates and by `linkedin_message` templates meant as InMails.
  It is `null` otherwise.
- `is_answered` is false when `answer` is empty or `TBD`.
- `[Name]`, `[Company]` and similar bracketed words are left as-is. They are filled in by hand or by
  a later phase.

### ProfileField

One detail about the candidate, stored in the database.

```json
{ "key": "current_company", "label": "Company", "section": "Current job", "value": "VerSe Innovation Pvt. Ltd. (Dailyhunt)", "position": 6 }
```

- `key` is unique, snake_case and stable. Code uses it to look up a value, for example the phone
  number when filling a form.
- `section` groups fields on the Profile page. Sections are shown in order of their lowest
  `position`, and fields are sorted by `position` within a section.
- Keys with a special meaning: `name`, `email`, `phone`, `location`, `current_company`,
  `designation`, `total_experience`, `current_ctc`, `graduation`, `linkedin_url`, `naukri_url`,
  and `skills` (a comma-separated list).
- The job-search priority is stored as three more keys. Each is a comma-separated list in priority
  order, and the search runs experience first, then roles, then locations:
  - `search_experience_order`, for example "1+ years, 2+ years, 3+ years"
  - `search_roles`
  - `search_locations`
  The Template Library page shows them.
- New fields can be added at any time with any key.

### Profile mirror file

`profile/CANDIDATE_PROFILE.md` (the `PROFILE_PATH` setting) is no longer read by the backend. It is
a **generated, read-only copy** of the profile fields and every template. The backend rewrites the
whole file after every profile or template change, so it always matches the database, and git
keeps the history.

- The file starts with a notice saying it is generated and must not be edited by hand.
- It has a section per profile section, with a `Field | Value` table, followed by the templates
  grouped by kind.
- Each template is a `###` heading (its question or name), followed by an optional
  `Subject: ...` line, followed by the full text as plain paragraphs (not inside a table cell).
- Writes are atomic.

### Connection

The login state of one platform. There is always exactly one per platform.

```json
{
  "platform": "naukri",
  "status": "connected",
  "account_name": "Harsh Vaghela",
  "checked_at": "2026-10-13T09:00:00Z",
  "message": null,
  "login_url": "https://www.naukri.com/nlogin/login"
}
```

- `status` is one of:
  - `connected`: a saved login works.
  - `disconnected`: there is no saved login, or it expired.
  - `connecting`: a login window is open and waiting for the user.
  - `error`: the check itself failed, for example the browser couldn't start. `message` says why.
- `account_name` is `null` when it can't be read.
- `login_url` is the platform's login page, which the frontend opens in the user's own browser.
- Logins live in a browser profile per platform under `BROWSER_PROFILES_DIR` (gitignored).
  Passwords are never stored by the app.

### ScanRun

One run of one scanner.

```json
{
  "id": 31,
  "kind": "naukri_jobs",
  "started_at": "2026-10-13T09:00:00Z",
  "finished_at": "2026-10-13T09:03:10Z",
  "status": "ok",
  "found": 42,
  "new": 7,
  "message": null
}
```

- `kind` is one of:
  - `naukri_jobs`: Naukri job search.
  - `naukri_apply`: applying to today's Naukri one-click jobs, when auto-apply is on. `found` counts
    the attempts and `new` counts the applications sent.
  - `linkedin_apply`: LinkedIn Easy Apply, when its auto-apply switch is on. It runs right after
    `linkedin_jobs`.
  - `linkedin_jobs`: LinkedIn job search **with the saved LinkedIn login**. Since 2026-10-13, logged-out
    searches get HTTP 429 or a login wall from the candidate's connection.
  - `linkedin_posts`: LinkedIn hiring posts with a Google Form or an email address. This one needs
    a login. Since 2026-10-15: 5 searches aimed at Google Forms (read further) before one search
    per role; Google Form posts may be up to 7 days old (the form searches look back a week),
    email posts only today; `lnkd.in` links are followed with a plain request (up to 15 per run) to find forms
    behind them; course and training adverts are left out. `message` counts the posts read, those
    with a form or an email, and why matching ones were left out.
- `status` is one of `running`, `ok`, `skipped`, `failed`.
  - `skipped` is used when a needed login is missing, and `message` says so.
  - `failed` is used when the run hit an error, a block or a CAPTCHA, and `message` says which.
- `found` counts the matching items seen today. `new` counts the ones not seen before.

## Endpoints

### Health

- `GET /api/health` returns `{ "status": "ok" }`.

### Dashboard

- `GET /api/dashboard/summary` returns:

```json
{
  "applied_today": 4,
  "applied_total": 57,
  "applied_by_platform": { "linkedin": 31, "naukri": 26 },
  "needs_action": { "company_site": 9, "google_form": 3 },
  "outreach_pending": 5,
  "unanswered_questions": 2,
  "caps": {
    "easy_apply_today":        { "used": 4,  "limit": 25 },
    "naukri_apply_today":      { "used": 0,  "limit": 50 },
    "connection_notes_month":  { "used": 40, "limit": 250 },
    "inmail_month":            { "used": 1,  "limit": 5 }
  }
}
```

- "Today" and "month" use the server's local timezone.
- The cap limits come from backend settings.
- `needs_action` counts jobs whose status is `needs_manual` or `found`, grouped by apply type.
  All five apply types are always present, including ones with a count of 0.
- `outreach_pending` counts messages with status `draft`.

- `GET /api/applications?platform=&limit=&offset=` returns `{ items: Job[], total }`.
  - It lists only jobs with status `applied`, newest `applied_at` first.
  - `platform` is optional.

### Jobs

- `GET /api/jobs?platform=&apply_type=&status=&limit=&offset=` returns `{ items: Job[], total }`.
  - All filters are optional. `status` can be repeated, e.g. `status=found&status=needs_manual`.
  - Results are sorted by `found_at` descending.
  - `found_after` is an optional ISO datetime. Only jobs found at or after it are returned.
    The Dashboard's "Found today" list uses it.
- `GET /api/jobs/{id}` returns a `Job`, or 404.
- `PATCH /api/jobs/{id}` takes a body of `{ "status": "applied" | "skipped" | "needs_manual", "status_reason"?: string }`
  and returns the updated `Job`.
  - Setting `applied` also sets `applied_at` to now. The UI uses this for the "Mark as applied" button
    on company-site and Google Form jobs.
  - Re-marking a job that is already applied keeps its original `applied_at`.
  - Setting any other status clears `applied_at`.
  - Leaving out `status_reason` clears it.
  - Any other status value returns 422.

### Connections

- `GET /api/connections` returns `{ items: Connection[], total }`. It always has both platforms,
  LinkedIn first.
- `POST /api/connections/{platform}/connect` returns the `Connection` with status `connecting`
  (status code 202).
  - It opens a **visible** Chrome window on the platform's login page, using that platform's saved
    browser profile. The user logs in there; 2FA, OTP and CAPTCHAs are handled by hand in that
    window.
  - The backend polls the window until it detects a login, then saves it, closes the window and sets
    the status to `connected`.
  - The status becomes `disconnected` (with a message) if the window is closed first or
    `LOGIN_TIMEOUT_MINUTES` (default 10) passes.
  - If a login window for that platform is already open, it returns 409.
- `POST /api/connections/{platform}/import` is the **main way to connect** (the candidate chose it on
  2026-10-13). It copies the login from the user's **own everyday Google Chrome**:
  1. The frontend opens `login_url` in a new tab of the user's browser. The user logs in there, or is
     already logged in.
  2. The frontend calls this endpoint repeatedly until the status is `connected`.
  3. Each call reads that platform's cookies from the user's Chrome. Only hosts ending in
     `linkedin.com` or `naukri.com` are read. The Chrome folder is `CHROME_USER_DATA_DIR` (default
     `~/Library/Application Support/Google/Chrome`); every Chrome profile there (`Default`,
     `Profile N`) is checked, and the most recently used one that has the login wins.
  4. It copies those cookies into the tool's own browser profile.
  5. It confirms the login as described under "Detecting a real login".
  - **Fast path:** cookie names can be read without decrypting anything. If none of them suggest a
    login, it returns `disconnected` with the message "Not logged in to <Platform> in your Chrome
    yet." It does not touch the Keychain in that case.
  - **Decrypting** needs the macOS Keychain item "Chrome Safe Storage", and macOS asks the user the
    first time. The key is then cached in memory.
    - If access is denied, or Chrome isn't found, it returns `error` with a clear message.
  - Cookie **values are never logged, returned or stored** anywhere except the tool's own browser
    profile. The backend log may list cookie names, to help tune detection.
  - It returns the `Connection`: `connected`, or `disconnected` or `error` with a message.
- `POST /api/connections/{platform}/connect` (the visible login window) stays as a **fallback** for
  when importing from Chrome doesn't work.
- **Detecting a real login.** A platform is `connected` only when **both** checks pass, so a URL that
  merely didn't redirect is never enough:
  1. The auth cookie is present. For LinkedIn that is `li_at`. Naukri's cookie names haven't been
     verified, so for Naukri the page check decides.
  2. A member-only page loads, in a headless browser, without redirecting to a login, auth-wall or
     checkpoint page, **and** its HTML shows logged-in content:
     - LinkedIn: `/feed/` shows the signed-in navigation or profile, not "Sign in" or "Join now".
     - Naukri: `/mnjuser/homepage` shows the user's profile widget or name, and the header does
       **not** show the "Login" or "Register" buttons.
- **Staying in sync.** Before every scan that needs a login, the backend silently imports from
  Chrome again, so the tool always follows the user's real browser login.
- `POST /api/connections/{platform}/disconnect` deletes that platform's saved browser profile and
  returns the `Connection` with status `disconnected`.
- **When the status is checked:**
  - when the backend starts, in the background
  - before every scan that needs the login
  - whenever a page redirects to a login wall during a scan, which sets `disconnected`

### Scans

The backend runs every scanner on a schedule: every `SCAN_INTERVAL_MINUTES` (default 30) while
`SCAN_ENABLED` is true.

- **What gets picked up.** Only jobs and posts from **today** in local time, using the platform's
  "past 24 hours" filter plus a check of each item's date. Matches must also:
  - have a title that matches a role in `search_roles` (synonyms count, for example
    Engineer/Developer, SDE1/SDE-1/SDE I, Golang/Go, Backend/Back End)
  - not be an internship
  - have a minimum experience of 3 years or less
  - be in one of the `search_locations`, where "Remote" uses the platform's remote filter
- **New items** are stored as `Job` rows with status `found` and the scanner fields filled in.
  Existing items (same platform and external id) are left alone.
- **Apply types:**
  - Naukri: `one_click` when the page shows Naukri's own Apply button, `company_site` when it says
    "Apply on company site".
  - LinkedIn jobs: `easy_apply` or `company_site`.
  - LinkedIn posts: `google_form`, with `form_url` set, or `email`, with `contact_email` set.
    Posts with neither are ignored.
- **Nothing is ever applied to or sent** by the scanners.
- **Pacing:**
  - random waits of 2 to 6 seconds between page loads
  - at most about 20 page loads per scanner per run
  - a run stops at the first block or CAPTCHA
- **Order:** one run at a time, with all scanners one after another, LinkedIn first (2026-10-14):
  `linkedin_jobs`, `linkedin_apply`, `linkedin_posts`, `email_send`, `linkedin_outreach`, `naukri_jobs`,
  `naukri_apply`. `GET /api/scans/status` lists `latest` in this order. The first automatic run
  is `SCAN_INTERVAL_MINUTES` after the previous run finished, or 1 minute after startup if there
  has been no run within the interval.

- `GET /api/scans/status` returns:

```json
{
  "enabled": true,
  "interval_minutes": 30,
  "running": false,
  "next_run_at": "2026-10-13T09:30:00Z",
  "latest": [ScanRun, ScanRun, ScanRun]
}
```

  `latest` holds the most recent run of each kind, in the scanner order above.
- `POST /api/scans/run` starts a run now and returns 202 `{ "started": true }`. It returns 409 if a
  run is already going. In a run started this way, LinkedIn's hourly job and post searches run
  when their last search was at least 15 minutes ago (2026-10-15); scheduled runs keep the hour.
- `GET /api/scans?limit=&offset=` returns `{ items: ScanRun[], total }`, newest first.

### Auto-apply (Naukri one-click)

The candidate switched this on on 2026-10-13. It covers only Naukri's own one-click "Apply". Nothing
on LinkedIn, and no email or message, is sent automatically.

- `GET /api/settings` returns `{ "auto_apply_naukri": false }`. It is off until the user turns it on.
- `PUT /api/settings` takes `{ "auto_apply_naukri"?: boolean }` and returns the settings.
- **After each Naukri scan** (scan kind `naukri_apply`), while the switch is on, the backend applies to
  today's Naukri jobs that have apply type `one_click` and status `found`.
  - Order: lowest `priority` first.
  - Limits: at most 10 per run, at most `NAUKRI_DAILY_CAP` (50) per day.
  - A random 20 to 45 second wait between applications.
  - It stops at a login wall, a block page, or Naukri's daily quota.
  - **Skip rules**, chosen by the candidate on 2026-10-13 (`app/sources/naukri/screening.py`). A job
    becomes `skipped`, with status reason "Not auto-applied: <reason>", when any of these holds:
    - the job page's salary is below the `current_ctc` profile field (a hidden salary is fine)
    - none of the `skills` profile field is in the job's key skills
    - it's a fresher or internship-style posting: 0 years' minimum experience plus a mention of
      intern, trainee, training or freshers
    - it's posted by a consultancy or an individual recruiter: a hidden company, a recruitment,
      placement, staffing or consultancy name, or a person's name
  - **What each result does to the job:**

    | Result | Job change |
    |---|---|
    | Applied, or Naukri says it's already applied | `status` `applied`, `applied_at` now |
    | It's a company-site job after all | `apply_type` becomes `company_site`; `status` stays `found` |
    | Naukri asks the recruiter's questions (the chatbot drawer) | Answered one by one from the profile and Template Library, as in Easy Apply (2026-10-14). All answered → `applied`. A question with no answer → `needs_manual`, with the questions in `status_reason`, and the question is added to unanswered questions. Questions are never guessed. Answers worked out from the profile are saved as Template Library answers. |
    | The click "fails" but the page goes to Naukri's `saveApply` page | `applied` only when its `multiApplyResp` code is 200 |
    | The job no longer takes applications | `skipped`, with Naukri's words |
    | No Apply button, or no confirmation after clicking | `needs_manual`, and a screenshot is saved to `backend/data/diagnostics/` |

- `POST /api/jobs/{id}/apply?dry_run=` applies to one job now, in the same way, and returns
  `{ result, detail, questions, job }`.
  - `result` is one of `applied`, `already_applied`, `questionnaire`, `company_site`,
    `quota_reached`, `no_button`, `no_confirmation`, `skipped` (a skip rule matched), or `would_apply`
    (a dry run: the Apply button was found but not clicked).
  - `facts` is what the job page showed: `company`, `posted_by`, `salary`, `experience`, `skills` and
    `description`. A dry run reports the skip rules too, but records nothing.
  - `facts.questions`: one row per recruiter question, `{ question, type, answer, source, options? }`
    (`answer` null = no answer). A dry run never clicks Apply (on a job without questions that click
    applies), so it previews the questions the job asked on its last try.
  - `facts.learned_answers`: answers worked out from the profile and saved to the Template Library.
  - **Errors:**
    - 422: the job isn't a Naukri one-click job.
    - 409: it's already applied, a scan is running, or Naukri isn't connected.
    - 502: Naukri blocked the page.

### LinkedIn search and Easy Apply

The candidate decided this on 2026-10-13.

- **Job search (`linkedin_jobs`)** uses the saved LinkedIn login and runs at most once every
  `LINKEDIN_SCAN_INTERVAL_MINUTES` (60).
  - It is skipped, with a message, when LinkedIn isn't connected.
  - It searches today's jobs (`f_TPR=r86400`) for the roles and locations, Easy Apply jobs included.
    Each job is stored as `easy_apply` or `company_site`.
  - At most about 10 page loads per run, with human pauses.
- **Easy Apply (`linkedin_apply`)** runs while the `auto_apply_linkedin` setting is on (default
  off; `GET`/`PUT /api/settings`).
  - It takes today's `easy_apply` jobs with status `found`, lowest `priority` first.
  - Limits: at most 5 per run and `EASY_APPLY_DAILY_CAP` (25) per day, with a random 45 to 90 second
    wait between applications.
  - It stops at a login wall, a security check or a block.
  - **Skip rules:** the same four as Naukri, adapted to LinkedIn's job page:
    - the salary, when the page shows one
    - your skills, matched against the job's title and description
    - fresher or internship postings
    - consultancies: a recruitment-style company name, or the industry "Staffing and Recruiting"
  - **The form.** Each step's fields are filled only from these sources, and nothing is guessed:

    | Field | Source |
    |---|---|
    | Name, email, phone, city | Profile |
    | Current CTC | `current_ctc` (12 LPA). Entered as 12 when the field is in lakhs, or as 1200000 in rupees. |
    | Expected CTC | `form_expected_ctc` (18 LPA) |
    | Total years of experience | `form_years_experience`: 1.9, or 2 when the field only accepts whole numbers |
    | Notice period | The "Notice period" template (30 days) |
    | Any other question | A Template Library answer whose question matches. Matching ignores case and punctuation and needs at least 85% word overlap. |

  - **When a field has no answer:** if any required field can't be answered, the application is
    **discarded, never submitted**. The job becomes `needs_manual` and its questions are added to
    unanswered questions. Answering them in the Template Library lets later jobs go through.
  - **When LinkedIn rejects an answer** (an inline error such as "Invalid input"): a text answer with
    a unit is sent again as the plain number ("12 LPA" -> 12, "30 days" -> 30; seen live on
    2026-10-14), a decimal again as a whole number when the error asks for one. An answer rejected
    a second time counts as no answer. Each box is tracked on its own, so a question asked twice is
    answered twice.
  - Answers worked out from the profile (e.g. years with a skill) are saved as Template Library
    answers and reused.
  - The resume LinkedIn already selected is used, and nothing is uploaded. "Follow company" is
    unticked before submitting.
  - **Results:** `applied` when LinkedIn confirms ("Application sent" / "Your application was
    sent"). Otherwise `needs_manual` with a screenshot.
- `POST /api/jobs/{id}/apply?dry_run=` also works for LinkedIn `easy_apply` jobs.
  - A dry run opens the form, fills every step it can, and reports each field: label, type,
    required, the value chosen, or "no answer".
  - A dry run **never submits**. It discards the draft at the end.
  - `facts` also includes `fields`, the list of form fields.

### Automatic email and LinkedIn outreach

The candidate decided on 2026-10-14 that these are **sent automatically**, within limits. Each has
its own switch, off until a checked test send.

**Email to hiring posts (`email_send`, after `linkedin_posts`)**, while the `auto_send_email`
setting is on:
- **Which jobs:** today's jobs with apply type `email`, status `found` and a `contact_email`, most
  important first.
- **The message:** built from the first non-empty `email` Template.
  - `[Job title]` becomes the job title with " (hiring post)" removed, and `[Company]` and `[Name]`
    are filled when known.
  - The resume at `RESUME_PATH` is attached.
- **Sending:** Gmail SMTP using the `SMTP_*` settings from `backend/.env`. `SMTP_APP_PASSWORD` is the
  app password; spaces in it are ignored.
- **Limits:**
  - at most 5 per run and `EMAIL_DAILY_CAP` (20) per day
  - never the same address twice within 30 days
  - 30 to 90 second waits between emails
- **Recording:** each email is stored as an `OutreachMessage` (kind `email`, status `sent`, with
  `sent_at`, or `failed` with the error), and the job becomes `applied` with the reason "Emailed
  <address>".
- **Missing setup:** when `SMTP_APP_PASSWORD` is empty or missing, the run is skipped with a message
  saying so.
- `POST /api/outreach/test-email` sends the email template, with the resume, **to the candidate's own
  `SMTP_USER`**. Its subject starts with "[TEST]" and `[Job title]` becomes "Software Engineer". It
  returns `{ "sent": true, "to": ... }`, or a 4xx/5xx with the SMTP error. Nothing is recorded.

**LinkedIn connection notes (`linkedin_outreach`, after `email_send`)**, while the
`auto_send_linkedin_notes` setting is on and LinkedIn is connected:
- **Who:** people found today from two sources (narrowed on 2026-10-27, see "Who gets notes and
  emails"):
  1. the "Meet the hiring team" section on LinkedIn job pages, for jobs the tool stored
  2. the authors of matching hiring posts
- **The note:** built from a `linkedin_message` Template, with `[Name]` (first name) and `[Company]`
  filled. Templates mentioning Easy Apply are used only for jobs that were applied to. The
  filled-in note must be at most 300 characters, otherwise that person is skipped. Since
  2026-10-27 only templates with both `[Job title]` and `[Company]` are used.
- **Sending:** from the person's profile, Connect (or More → Connect) → "Add a note" → fill → Send.
  - If "Connect" isn't available (already connected, pending, or follow-only), the person is
    skipped.
  - InMails are not sent automatically.
- **Limits:**
  - at most 3 per run and 12 per day, staying under `CONNECTION_NOTES_MONTHLY_CAP` (250) a month
  - never the same profile twice
  - 60 to 180 second waits between notes
- **Recording:** each note is stored as an `OutreachMessage` (kind `connection_note`, with
  `recipient_name`, `recipient_profile_url`, the body, and status `sent` or `failed`), linked to the
  job when known.
- `POST /api/outreach/test-note?profile_url=...&dry_run=true` opens the profile and the "Add a note"
  dialog and fills the note, but **does not send** when `dry_run` is true. It returns the note and the
  buttons it found. With `dry_run=false`, it sends that one note.

**Shared:**
- New settings flags `auto_send_email` and `auto_send_linkedin_notes`, both off by default, in
  `GET`/`PUT /api/settings`.
- Settings flag `pause_naukri` (off by default; on since 2026-10-14 while LinkedIn is fixed): while on,
  `naukri_jobs` and `naukri_apply` end `skipped` with "Naukri is paused." without opening a browser.
  Applying to one job from the app still works.
- New scan kinds `email_send` and `linkedin_outreach`, run last in that order.

### Outreach queue

- `GET /api/outreach?kind=&status=&limit=&offset=` returns `{ items: OutreachMessage[], total }`,
  newest first.
- `PATCH /api/outreach/{id}` takes a body of `{ "subject"?: string, "body"?: string }`.
  - It only works while the message is a `draft`; otherwise it returns 409.
  - It returns the updated message.
- `POST /api/outreach/{id}/approve` moves the message from `draft` to `approved` and sets
  `approved_at`. Any other starting status returns 409.
- `POST /api/outreach/{id}/reject` moves the message from `draft` or `approved` to `rejected`.
  Any other starting status returns 409.
- Nothing is actually sent in Phase 1. The sending workers (Phase 4 and 5) pick up `approved` messages.

### Template Library

Templates are stored in the database (the `Template` model).

- `GET /api/templates?kind=` returns `{ items: Template[], total }`.
  - `kind` is optional.
  - Results are sorted by kind (`answer`, then `linkedin_message`, then `email`), then by `id`.
- `POST /api/templates` takes a body of
  `{ "kind"?: TemplateKind (default "answer"), "question": string, "answer": string, "subject"?: string | null }`.
  - It returns the created `Template` with status 201.
  - `question` must not be blank.
- `PUT /api/templates/{id}` takes the same body and returns the updated `Template`, or 404.
  - A `kind` that is left out keeps its current value.
- `DELETE /api/templates/{id}` returns 204, or 404.
- Every create, update and delete regenerates the mirror file.

### Profile

The profile is stored in the database (the `ProfileField` model). The frontend shows it behind
the avatar at the top right and on the `/profile` page.

- `GET /api/profile` returns:

```json
{
  "summary": {
    "name": "Harsh Vaghela",
    "email": "candidate@example.com",
    "phone": "9000000001",
    "location": "Bengaluru",
    "current_company": "VerSe Innovation Pvt. Ltd. (Dailyhunt)",
    "designation": "Software Development Engineer 1",
    "total_experience": "1.9+ years",
    "current_ctc": "12 LPA",
    "graduation": "Mathematics and Computing, Dhirubhai Ambani University (formerly DA-IICT), 2021–2025",
    "linkedin_url": "https://www.linkedin.com/in/...",
    "naukri_url": "https://www.naukri.com/mnjuser/profile",
    "skills": ["Java", "Spring Boot", "Golang"],
    "resume": { "filename": "Harsh_Vaghela.pdf", "url": "/api/profile/resume" }
  },
  "sections": [
    {
      "title": "Personal",
      "fields": [{ "key": "name", "label": "Name", "section": "Personal", "value": "Harsh Vaghela", "position": 1 }]
    }
  ]
}
```

- Each summary value comes from the field with the matching key. It is `null` when that field is
  missing or empty.
- `skills` is the `skills` field split on commas and trimmed, with empty items removed.
- `resume` is `null` when the file at `RESUME_PATH` doesn't exist.
- `url` is relative to the API base URL.
- `sections` lists every field, grouped and ordered as described under `ProfileField`.

- `PUT /api/profile/fields` takes a body of
  `{ "fields": [{ "key", "label", "section", "value", "position"? }] }`.
  - It inserts or updates each field by `key`. A `position` that is left out keeps its current value.
    For a new field it becomes max + 1.
  - It returns the full profile, in the same shape as `GET /api/profile`.
  - It regenerates the mirror file.
  - `key` must be snake_case (`^[a-z][a-z0-9_]*$`); otherwise it returns 422.
- `DELETE /api/profile/fields/{key}` returns 204, or 404. It regenerates the mirror file.
- `GET /api/profile/resume` returns the resume file (`application/pdf`, shown inline, with its
  filename), or 404.
  - The file's location comes from the `RESUME_PATH` setting.
  - The default is `../profile/resume/Harsh_Vaghela.pdf`, resolved relative to `backend/`.

### Unanswered questions

- `GET /api/questions/unanswered` returns `{ items: UnansweredQuestion[], total }`. It lists only
  questions whose `answered_at` is null.
- `POST /api/questions/{id}/answer` takes a body of `{ "answer": string }` and returns the created
  `Template` with status 201.
  - It adds a Template Library entry (kind `answer`) with the question and answer, and sets
    `answered_at`.
  - If the question was already answered, it returns 409.

## UI redesign (2026-10-15)

The candidate asked for a simpler UI with two job states, newest jobs first, per-feature pause
switches and an Analysis tab. These additions apply on top of the sections above.

### Job order and the two states

- `GET /api/jobs` takes `sort=posted|found` (default `posted`).
  - `posted`: newest `posted_at` first; a job without `posted_at` counts by its `found_at`. Ties by
    `id` descending.
  - `found`: newest `found_at` first (the Dashboard's "All jobs").
- The UI shows two states only: **Applied** (`status` `applied`) and **Pending** (every other status).
- `PATCH /api/jobs/{id}` also accepts `"status": "found"`, shown as Pending: it clears `applied_at`
  and `status_reason`; a reason sent in the body is ignored. A Pending Easy Apply or one-click job
  found today is tried again by the next scan; older ones stay for the candidate.

### Pause switches

`GET`/`PUT /api/settings` gain four flags, all `false` by default:

| Flag | Pauses |
|---|---|
| `pause_linkedin` | every LinkedIn scanner: `linkedin_jobs`, `linkedin_apply`, `linkedin_posts`, `email_send`, `linkedin_outreach` |
| `pause_linkedin_jobs` | `linkedin_jobs` (the job search) |
| `pause_linkedin_posts` | `linkedin_posts` (hiring posts with a Google Form or email) |
| `pause_naukri_jobs` | `naukri_jobs` (the Naukri search) |

- `pause_naukri` (existing) pauses `naukri_jobs` and `naukri_apply`.
- The other features keep their switches: `auto_apply_linkedin` (Easy Apply), `auto_send_email`,
  `auto_send_linkedin_notes`, `auto_apply_naukri` (one-click).
- A paused scanner records `skipped` with "LinkedIn is paused." / "Naukri is paused." or
  "<feature> is paused." and opens no browser (the platform message wins when both are on; this is
  checked before anything else, so missing search settings don't fail a paused scanner). Applying
  to one job from the app still works.

### Scan intervals

`GET`/`PUT /api/settings` also carry two intervals in minutes (2026-10-15), shown as editable boxes
in the scanner panel:

- `scan_interval_minutes` (10–240): every scan run. The default is `SCAN_INTERVAL_MINUTES` (30).
  Saving it plans the next run again from the last finished one.
- `linkedin_scan_interval_minutes` (30–720): LinkedIn's job and post searches. The default is
  `LINKEDIN_SCAN_INTERVAL_MINUTES` (60). Runs in between skip them with "Next LinkedIn search after
  HH:MM.".
- A value outside its range returns 422. `GET /api/scans/status` reports the current
  `interval_minutes`.

### Analysis

`GET /api/analytics` returns counts for two periods, `today` (jobs found today, the server's local day) and
`all`:

```json
{
  "today": {
    "linkedin": {
      "easy_apply":   { "found": 12, "applied": 5, "pending": 7 },
      "company_site": { "found": 30, "applied": 2, "pending": 28 },
      "google_form":  { "found": 1,  "applied": 0, "pending": 1 },
      "email":        { "found": 6,  "applied": 5, "pending": 1 },
      "connection_notes_sent": 0
    },
    "naukri": {
      "one_click":    { "found": 9, "applied": 4, "pending": 5 },
      "company_site": { "found": 3, "applied": 0, "pending": 3 }
    }
  },
  "all": { "...": "the same shape, all time" }
}
```

- `found`: jobs of that platform and apply type (for `today`, found today). `applied`: those with
  status `applied` (for `email`: emailed). `pending`: `found - applied`.
- `connection_notes_sent`: outreach messages of kind `connection_note` with status `sent` (for
  `today`, sent today).
- The caps and unanswered-question counts stay in `GET /api/dashboard/summary`.

## Separate LinkedIn and Naukri scans (2026-10-15)

The candidate asked for LinkedIn and Naukri to be scanned separately, each shown in its own section
of the scanner panel. This replaces the single schedule described under "Scans" and the "Scan
intervals" section above.

- **Two scan groups**, each with its own schedule, "Run now" and status:
  - `linkedin`: `linkedin_jobs`, `linkedin_apply`, `linkedin_posts`, `email_send`, `linkedin_outreach`
  - `naukri`: `naukri_jobs`, `naukri_apply`
- Within a group, one run at a time, its scanners one after another in that order. The two groups
  are independent and may run at the same time (each platform has its own browser).
- A group's next automatic run is its interval after its previous run finished (manual runs count
  too). After a (re)start it is planned from that group's last finished run, or 1 minute after
  startup if there was none within its interval.
- Backoff after failures uses the scanner's own group interval.

### Settings

`GET`/`PUT /api/settings` carry three intervals in minutes (they replace `scan_interval_minutes` and
`linkedin_scan_interval_minutes`):

| Key | Range | Default | What |
|---|---|---|---|
| `linkedin_scan_interval_minutes` | 10–240 | `SCAN_INTERVAL_MINUTES` (30) | LinkedIn scan runs |
| `naukri_scan_interval_minutes` | 10–240 | `SCAN_INTERVAL_MINUTES` (30) | Naukri scan runs |
| `linkedin_search_interval_minutes` | 30–720 | `LINKEDIN_SEARCH_INTERVAL_MINUTES` (60; the old env name `LINKEDIN_SCAN_INTERVAL_MINUTES` still works) | LinkedIn's job and post searches inside LinkedIn runs |

- A value outside its range returns 422. Saving a scan interval plans that group's next run again.

### Endpoints

- `GET /api/scans/status`:

```json
{
  "enabled": true,
  "running": false,
  "next_run_at": "2026-10-15T09:59:00Z",
  "interval_minutes": 30,
  "platforms": {
    "linkedin": { "running": false, "interval_minutes": 30, "next_run_at": "2026-10-15T09:59:00Z", "last_finished_at": "2026-10-15T09:29:00Z" },
    "naukri":   { "running": true,  "interval_minutes": 30, "next_run_at": null, "last_finished_at": "2026-10-15T09:20:00Z" }
  },
  "latest": []
}
```

  - `running`: any group is running. `next_run_at`: the earliest next run of the two (null when
    both are running or scanning is disabled). `interval_minutes`: LinkedIn's (kept for older UIs).
  - Per group: `next_run_at` is null while it runs or when scanning is disabled.
- `POST /api/scans/run?platform=linkedin|naukri` starts that group now (a manual run: LinkedIn's
  searches run if the last one was at least 15 minutes ago). Without `platform` it starts every
  group that isn't running. Returns 202 `{ "started": true, "platforms": ["linkedin"] }`, or 409
  when the requested group (without `platform`: both) is already running. An unknown platform is 422.
- Applying to one job from the app (`POST /api/jobs/{id}/apply`) and the test note
  (`POST /api/outreach/test-note`) return 409 only while that job's platform group is running.

## Deployment: login, pushing logins and data (2026-10-15)

The candidate deploys the frontend on Vercel and the backend on Render (a Docker service with a
persistent disk). These additions make that safe; locally (no `APP_PASSWORD`) nothing changes.

### App login

- Backend settings: `APP_PASSWORD` (empty = no login, the local default) and `AUTH_SECRET` (signs
  tokens; when empty it is derived from `APP_PASSWORD`).
- `GET /api/auth/status` (always public) returns `{ "required": bool, "authenticated": bool }`.
- `POST /api/auth/login` takes `{ "password": string }` and returns `{ "token": string,
  "expires_at": datetime }` (30 days). A wrong password is 401. After 5 wrong passwords from one
  client address within 15 minutes, further attempts get 429 for 15 minutes.
- While a login is required, every other `/api/*` request needs `Authorization: Bearer <token>`;
  without a valid one it gets 401 `{ "detail": "Login required." }`. Always public: `GET
  /api/health`, `/api/auth/*`, and CORS preflight (`OPTIONS`). 401 responses carry CORS headers.
- The token is an HMAC-SHA256-signed value with its expiry; changing `APP_PASSWORD` or `AUTH_SECRET`
  logs every device out.
- Frontend: when `GET /api/auth/status` says required and not authenticated, or any call returns
  401, it shows a password screen. The token is kept in `localStorage` and sent on every call; the
  profile menu has "Log out" (drops the token).

### Pushing LinkedIn / Naukri logins from the Mac

- `PUT /api/connections/{platform}/cookies` takes `{ "cookies": [ { "name", "value", "domain",
  "path", "expires", "httpOnly", "secure", "sameSite" } ] }` (Playwright's cookie shape). Only
  cookies of that platform's domain are kept. The backend installs them into the platform's browser
  profile, checks the login and returns the `Connection` (as `POST .../import` does).
- `POST /api/connections/{platform}/import` on a machine without the user's Chrome returns 409
  "Chrome isn't on this server: push the login from your Mac." The same connection is then kept fresh
  only by pushing again.
- The command (run on the Mac, from `backend/`): `.venv/bin/python -m app.tools.push --server
  https://<backend> login linkedin|naukri|all` - reads the login from the Mac's Chrome (the same
  code as Connect; macOS asks for "Chrome Safe Storage" access) and sends it. It asks for the app
  password (or reads `JOB_APPLY_PASSWORD`).

### Uploading the resume and the database

- `PUT /api/admin/resume` takes the PDF as the raw body (`Content-Type: application/pdf`, at most
  10 MB, must start with `%PDF`) and saves it to `RESUME_PATH`. Returns `{ "bytes": int }`.
- `PUT /api/admin/database` takes a SQLite file as the raw body (`application/octet-stream`, at most
  100 MB). Refused (409) while a scan is running. It checks the file is a SQLite database with this
  app's tables, replaces the database, adds missing columns, and returns `{ "jobs": int,
  "templates": int, "profile_fields": int }`.
- Commands: `python -m app.tools.push --server ... resume [path]` (default: the local
  `RESUME_PATH`) and `... database [path]` (default: a consistent snapshot of the local database).
- New setting `DIAGNOSTICS_DIR` (default `backend/data/diagnostics`), so screenshots can go to the
  persistent disk.

### Deployment details (as built, 2026-10-15)

- `POST /api/auth/login` returns 400 when no `APP_PASSWORD` is set.
- The wrong-password limit counts per client address - the **last** `X-Forwarded-For` entry (the
  one Render's proxy adds; earlier entries can be made up) - and across all addresses: 20 wrong
  passwords within 15 minutes block every login for 15 minutes. 429 responses carry `Retry-After`.
- `PUT /api/connections/{platform}/cookies`: expired cookies are dropped; 422 when none of the
  cookies is a login (the saved login stays); 409 while another import or push of that platform runs.
- `PUT /api/admin/resume` and `/database`: 415 for a wrong `Content-Type`, 413 when too large, 422
  for an invalid file. The database upload is copied into the live database in one transaction
  (SQLite backup API) instead of replacing the file; the server keeps its own `connection` rows (the
  logins it holds), closes runs the upload shows as running, and re-reads the scan intervals.

## Job fit and outreach accuracy (2026-10-15)

The candidate asked for fewer, better-fitting applications to get interview calls sooner.

### Fit rules (both platforms)

Applied when a job is found (LinkedIn and Naukri searches: the job isn't stored; the run message
says "N left out: X another stack, Y senior, Z on your skip list.") and again before auto-apply
(status reason "Not auto-applied: ..."):

| Rule | Reason shown |
|---|---|
| The company contains an entry of the profile field `search_skip_companies` (comma-separated, editable in the Template Library; starts with Jobgether, Crossing Hurdles, Scoutit, Applicantz, CodeRound AI) | `<company> is on your skip list.` |
| The title names another stack (Android, iOS, .NET/C#, PHP, Ruby, Salesforce, SAP, Oracle, Mainframe, QA/Test/SDET, Support, Embedded, Games, Data Annotation, frontend-only, Kotlin, ...) and none of the candidate's skills | `Another stack: the title says "<term>" and names none of your skills.` |
| The title is senior (Senior, Sr., Lead, Principal, Staff, Architect, Manager, Head, Director, VP, SDE 3 / III, L3); SDE 2 / Software Engineer II are kept | `Too senior: the title says "<term>".` |
| Fewer than 2 of the candidate's skills in the title and description (Naukri: also key skills), when those are known | `Only 1 of your skills (<skill>) is in <where>; at least 2 are needed.` |

- Hiring posts: the skip list (the author's company) and the senior rule on the role line; the
  email sender also skips stored posts of a skip-listed company ("Not emailed: <company> is on your
  skip list.").

### Connection notes on LinkedIn's 2026-10-15 profile page

- The profile page has no top card or `<h1>`: the name comes from the page title, and Connect is a
  link `<a aria-label="Invite <Name> to connect" href="/preload/custom-invite/?vanityName=<slug>">`.
  Only the link with the profile's own vanityName is used (other people's Connect links are on the
  page too).
- The invitation opens inside LinkedIn's hidden preload frame, so the tool opens that invitation
  page itself: "Add a note" -> the note -> "Send invitation".
- The search box's suggestions popover (`role="dialog"`, `data-testid="popover-floating"`) isn't
  the invitation dialog.
- Notes that failed with "no Connect button" before the fix (2026-10-15 12:20 UTC) are tried once
  more.

## Faster, more relevant applying (2026-10-21)

The candidate (now on Naukri Pro, no interview calls yet) restated the targets: Bengaluru,
16-18 LPA; first 1+ years as SDE / Software Development Engineer / Backend Engineer / Java
Developer / Full Stack Engineer, then 2+ years as SDE2 / SDE1 and the same roles. Skills: Java,
Spring Boot, Golang, React, GCP, GKE, Kubernetes, Docker, CI/CD, Redis, SQL, Cassandra, Kafka (plus
Python, Jenkins, Grafana). Stored as profile fields (`skills`, `search_*`, new `search_salary_lpa`
= "16-18 LPA").

### Salary rule (both platforms)

- Skip when the job's stated salary range tops out below the lower bound of `search_salary_lpa`
  (16 LPA): "Salary <range> is below your target 16 LPA." Without `search_salary_lpa` the current
  CTC is used as before. A job without a stated salary is kept.

### Answers nobody saved (Easy Apply and Naukri's recruiter questions)

The candidate chose (2026-10-21) to let forms finish without waiting for them. When neither the
profile, a template nor the profile-based rules answer a required question:

| Question | Answer |
|---|---|
| Availability, consent or willingness: interviews on any day or in any mode (Saturday, weekend, virtual, face-to-face), background verification, relocating or commuting within Bengaluru, consenting to a recruiter profile / communications, working from office / hybrid in Bengaluru | Yes |
| Self-rating ("Rate yourself out of 5/10 in X") | 4/5 (8/10) for one of the candidate's skills, 2/5 (4/10) otherwise; for options the closest one |
| Years with a tool or skill that isn't the candidate's | 0 (as before) |
| Free text with no saved answer, or anything else | No answer: the job is skipped ("The form asks <question>, which has no saved answer."), not left waiting |

- These answers are marked `source: "default (2026-10-21 policy)"` in the attempt's fields and are
  saved to the Template Library like other learned answers, so the candidate can change them.
- Questions about another city (relocating to Pune, etc.) keep the existing No rule.

### LinkedIn

- `linkedin_posts` also scrolls the home feed (`https://www.linkedin.com/feed/`) for hiring posts
  with a Google Form or an email (posts from the candidate's network), before the searches. Same
  matching rules; the run message counts feed posts separately.
- Company-site jobs get their company apply link promptly: each LinkedIn run opens up to 10 new
  company-site jobs without an `apply_url` and stores the "Apply on company website" target, so the
  Company Website list opens the company's page directly. Easy Apply jobs that turn out to be
  company-site get theirs at once (as before).

### Naukri

- The Naukri search uses Naukri's own filters: experience (from `search_experience_order`),
  location (`search_locations`), salary (the band containing the target), freshness (1 day), one
  search per role in priority order; plus the logged-in "recommended jobs" list (Naukri's matches for
  the candidate's profile). Same fit rules.

### Removing a job

- Each job row has a remove (×) button: `PATCH /api/jobs/{id}` with `{ "status": "skipped",
  "status_reason": "Removed by you." }`. Job lists show only `found`, `needs_manual` and `applied`
  jobs (the frontend passes these statuses), so removed and rule-skipped jobs disappear.
- `GET /api/analytics`: `found` counts jobs that aren't `skipped` or `failed`; `pending` is still
  `found - applied`.

### As built (2026-10-21)

- Default answers cover only the table's categories. Night shifts, bonds, pay, travel, other cities
  and past interviews get none (the job is skipped). Self-ratings are answered only when the scale
  is numeric; word levels ("Beginner/Expert") get none.
- A skipped form's questions still go to unanswered questions. Run messages: "N skipped for a
  question without a saved answer" (LinkedIn), "N skipped for a recruiter question without a saved
  answer" (Naukri); the Naukri applier pauses after those too.
- Company links: jobs found in the past 2 days, at most 10 per run (also when Easy Apply is off). A
  page without a company link sets "No company apply link on the LinkedIn job page." and isn't
  reopened; a closed job becomes skipped; the hiring team found there is stored for outreach.
  Message: "Found company links for N jobs."
- Feed: up to 7 page loads ("Read N feed posts and M search posts; ..."); `POST_MAX_PAGE_LOADS` is 44.
  A feed read with 0 posts saves a diagnostic page and is noted, not failed.
- Naukri: one search per role in priority order, then the logged-in recommended list
  (`/mnjuser/recommendedjobs`); message "Search: N jobs read; Recommended: M; K matched.", plus
  "Today's jobs not matched: N not one of your roles, M more experience than yours, K outside
  Bengaluru." (examples in the log). Live on 2026-10-21 the URL filters `experience=<years>` (one
  search per experience level) and `jobAge=1` were confirmed and are on (14 matches instead of 1-3);
  the salary band (`ctcFilter`) stays off because it hides jobs without a stated salary.

### Email formatting (2026-10-23)

- The email template may use light formatting: `**bold**`, lines starting with `- ` as bullet lists,
  and blank lines between paragraphs. Each email is sent as `multipart/alternative` (plain text
  without the `**` markers, and HTML with `<strong>`, `<ul>` and paragraphs) plus the resume PDF.
- The email's subject is "[Job title] Opportunity - Harsh Vaghela"; the body is the candidate's own
  text (2026-10-23, confirmed), with their bold parts; two blank lines make a bigger gap. Text is
  Gmail's default (Arial 13px), full width.

### Email addresses and posts abroad (2026-10-23)

- Addresses found in posts are cleaned: a word glued on after the ending is cut ("gmail.comsubject"
  -> "gmail.com"), "contact-" / "email:" and similar prefixes are dropped, and an ending that is
  neither a country code nor a known generic one is rejected ("node@wall.counter").
- A hiring post that names no preferred location is left out as "abroad" when it names a place
  abroad (Dubai, London, Jakarta, ...) or when every address belongs to a company abroad (a country
  ending other than .in: .co.id, .ae, .co.uk, .com.au, .sg, .my, .us, ...; .co, .io, .ai and similar
  count as generic).
- The email sender checks stored posts the same way before sending: "Not emailed: <address> isn't a
  usable email address." / "Not emailed: <address> belongs to a company abroad."; the run message
  counts them as "N skipped (abroad or not a real address)".

### Who gets notes and emails (2026-10-27)

Replies to the first notes and emails showed many went to the wrong people: job seekers, recruiters
hiring only abroad, people who aren't recruiters, and reshared job alerts. Since 2026-10-27 only
people hiring for one of the candidate's roles, in **Bengaluru or remote in India**, are written to.

- **Hiring posts:**
  - A job seeker's post ("open to work", "I'm looking for my next opportunity", internships done,
    "Hello recruiters, consultants available") is never kept, even with a Google Form. The new
    `linkedin_posts` left-out reason is "a job seeker's post".
  - A post with an email and no Google Form is kept, and so emailed, only when all of these hold:
    - it isn't a job alert or another company's opening ("Uber is hiring" from someone at another
      company, "job updates" channels, several "Company:" lines);
    - it names Bengaluru, or remote work in India;
    - its author is hiring: their headline shows a hiring role (recruiter, talent acquisition, HR,
      founder, CTO, engineering manager, ...) or the post says so ("we're hiring", "my team", "join
      us").

    The left-out reasons for these are "a job alert or another company's opening (email)", "not in
    Bengaluru or remote in India (email)" and "not posted by someone hiring (email)".
  - The author gets a connection note only when the same rules hold and the company is known (from
    their headline). Otherwise the job keeps no contact. A Google Form post is still listed for the
    candidate.
- **"Meet the hiring team":**
  - only for jobs whose location is in Bengaluru or remote in India; a plain "Remote" location
    doesn't count;
  - never someone whose headline says they're looking for a job.
- **The note** always names the role and the company:
  - It uses the job's title, or its shorter matched role when the note would be over 300 characters.
  - A job whose role or company isn't known is skipped: "N skipped (role or company not known)".
  - The candidate's note templates got `[Job title]` and `[Company]` added; the rest of the text is
    unchanged.
- **Send confirmation:**
  - LinkedIn's pages carry hidden translations such as "Sorry, something went wrong", which were read
    as an error after every Send. Hidden elements are now ignored, and errors are read only from the
    invitation dialog and LinkedIn's toasts.
  - On the invitation page, the dialog closing with no error counts as sent.
  - The 116 notes recorded as "LinkedIn said 'something went wrong' after Send" since 2026-10-21 are
    now `sent`: replies to them came back. They count toward the caps.

