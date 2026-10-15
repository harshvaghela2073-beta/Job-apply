# Job-apply backend

FastAPI + SQLModel API for the job-application automation app. Phase 1 serves the dashboard,
job lists, outreach approval queue, Template Library and candidate profile to the React frontend.
It also keeps the LinkedIn / Naukri logins and scans both sites every 30 minutes for jobs and hiring
posts from today. The scanners only find and store; applying (Naukri one-click, LinkedIn Easy
Apply) and sending (emails to hiring posts, LinkedIn connection notes) happen only while their own
switch is on (`GET`/`PUT /api/settings`, all off by default).
The HTTP contract is defined in the repository's `docs/API_CONTRACT.md`.

This directory is a self-contained app: it has its own dependencies, configuration and tests, and
can be deployed without the frontend.

## Setup

Requires Python 3.13.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional; defaults work for local development
```

The browser automation uses Playwright with the installed Google Chrome (`BROWSER_CHANNEL=chrome`),
so no browser download is needed. Without Chrome, set `BROWSER_CHANNEL=chromium` and run
`python -m playwright install chromium`.

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

- Health check: <http://localhost:8000/api/health>
- Interactive API docs: <http://localhost:8000/docs>

Missing tables are created on startup with `SQLModel.metadata.create_all`. New **nullable**
columns are then added to existing tables with `ALTER TABLE ... ADD COLUMN` (`app.db.add_missing_columns`);
nothing else about an existing table is ever changed. Anything bigger needs a real migration
(Alembic).

## Logins and scans

- **Logins.** Each platform has its own Chrome profile in `BROWSER_PROFILES_DIR/<platform>`
  (gitignored) that keeps the login; passwords never reach the app.
  - `POST /api/connections/{platform}/import` (the main way): the user logs in in their own Chrome,
    and the backend copies that platform's cookies (hosts ending in `linkedin.com` / `naukri.com`
    only) from `CHROME_USER_DATA_DIR` into the tool's profile. Cookie names are read first without
    decrypting; decrypting needs the macOS Keychain item "Chrome Safe Storage" (macOS asks the
    first time; the key is then kept in memory only). Cookie values are never logged, returned or
    stored anywhere but the tool's profile.
  - `POST /api/connections/{platform}/connect` (fallback): a visible Chrome window on the tool's
    profile; the user logs in there.
  - A login counts only when the auth cookie is there (LinkedIn `li_at`) **and** the session is
    confirmed. For LinkedIn the confirm is an authenticated call to `/voyager/api/me` (so it doesn't
    depend on page markup, which LinkedIn changes often); for Naukri a member page
    (`/mnjuser/homepage`) must show a logged-in member and not the Login / Register buttons. A
    `/checkpoint` challenge is reported as `error` asking the user to finish it in their Chrome.
  - When cookies are found in Chrome but the session can't be confirmed, the status is `error`
    (not `disconnected`), so the frontend stops polling. Identical cookies aren't re-verified more
    often than once a minute. A failed confirm logs the final URL, page title and marker hits and
    saves a screenshot under `backend/data/diagnostics/` (gitignored).
- **Scans** (`app/scheduler`, `app/sources/<platform>`): while `SCAN_ENABLED`, in two groups that
  run independently, each in its own thread and on its own interval (`linkedin_scan_interval_minutes`
  and `naukri_scan_interval_minutes` in `GET`/`PUT /api/settings`, default `SCAN_INTERVAL_MINUTES`);
  within a group one run at a time. LinkedIn scanners in order: `linkedin_jobs` (the
  logged-in job search, at most every `LINKEDIN_SEARCH_INTERVAL_MINUTES`; skipped when LinkedIn isn't
  connected; one rendered results page per location, the rest through LinkedIn's own job-data
  request, `app/sources/linkedin/search_api.py`), `linkedin_apply` (Easy Apply, only while
  `auto_apply_linkedin` is on; `app/sources/linkedin/easy_apply.py`, answers from `answers.py`,
  skip rules in `screening.py`), `linkedin_posts` (needs the LinkedIn login; skipped otherwise),
  `email_send` and `linkedin_outreach` (below); Naukri: `naukri_jobs` and `naukri_apply`.
  Matching rules, pacing (random 2-6 s pauses, `SCAN_MAX_PAGE_LOADS` per scanner) and priorities
  are in `app/scanner/matching.py`; every site-specific selector is in
  `app/sources/<platform>/selectors.py`. Each run is a `ScanRun` row; new matches become `Job` rows
  with status `found`.
- Browser work runs on one thread per platform (plus one for public pages), so a profile is never
  open twice; each task closes its browser when it ends.

## Automatic outreach

Decided by the candidate on 2026-10-14 (`docs/API_CONTRACT.md`, "Automatic email and LinkedIn
outreach"). Each sender has its own switch, off until a checked test send.

- **Emails to hiring posts** (`email_send`, `app/scanner/email_send.py`, while `auto_send_email` is
  on): today's `email` jobs with status `found`, most important first. The message is the first
  non-empty `email` template with `[Job title]` (without " (hiring post)"), `[Company]` and `[Name]`
  filled (`app/services/outreach.py`), and the resume at `RESUME_PATH` attached, sent through Gmail
  SMTP with STARTTLS (`app/services/mailer.py`). At most 5 per run and `EMAIL_DAILY_CAP` a day, never
  the same address twice within 30 days (that job becomes `skipped`), 30-90 s between emails. Each
  email is an `OutreachMessage` (`sent`, or `failed` when the server refused it) and the job becomes
  `applied` ("Emailed <address>"). A login or connection failure stops the run without recording
  anything, so the jobs are tried again later. No `SMTP_APP_PASSWORD` = skipped with a message.
- **LinkedIn connection notes** (`linkedin_outreach`, `app/sources/linkedin/outreach.py`, while
  `auto_send_linkedin_notes` is on and LinkedIn is connected): people found today: the authors of
  matching hiring posts (stored by `linkedin_posts`) and "Meet the hiring team" on the job pages of
  today's LinkedIn jobs (a few pages per run, each once; Easy Apply records it from the page it
  already has open). The note is a `linkedin_message` template (no InMail ones, no alumni ones; the
  "Easy Apply" one only for jobs applied to with Easy Apply) with `[Name]` (first name) and
  `[Company]` filled; over 300 characters = skipped. From the profile: Connect (or More -> Connect)
  -> "Add a note" -> fill -> Send. No Connect button = skipped (recorded so the profile isn't
  visited again); "weekly invitation limit" stops the run and pauses notes for a day; no
  personalized notes left this month pauses them until next month; a login wall marks LinkedIn
  disconnected. At most 3 per run, 12 a day and `CONNECTION_NOTES_MONTHLY_CAP` a month, never the
  same profile twice, 60-180 s between notes. InMails are never sent automatically.
- **Test sends first:** `POST /api/outreach/test-email` sends the email template (subject starting
  "[TEST]", `[Job title]` = "Software Engineer") with the resume to your own `SMTP_USER`; nothing is
  recorded. `POST /api/outreach/test-note?profile_url=...&dry_run=true` opens the profile, fills the
  note and closes the dialog without sending, and returns the note and the buttons it found;
  `dry_run=false` sends that one note.

## Storage

The database is the source of truth for everything, including the candidate's details:

- **Profile**: `ProfileField` rows (`key`, `label`, `section`, `value`, `position`). `key` is a
  stable snake_case name that code uses to look a value up (e.g. `phone` when filling a form).
- **Template Library**: `Template` rows of kind `answer` (application questions),
  `linkedin_message` or `email`.
- **Profile mirror file** (`PROFILE_PATH`, default `../profile/CANDIDATE_PROFILE.md`): a
  generated, read-only markdown copy of the profile fields and every template. The backend never
  reads it. After every profile or template write (including answering an unanswered question) the
  whole file is regenerated from the database with an atomic write, so git keeps its history. Edits
  made by hand are overwritten. If the file can't be written, the error is logged and the API
  request still succeeds.
- **Resume** (`RESUME_PATH`): served as-is by `GET /api/profile/resume`.

## Seed sample data

```bash
python -m app.seed           # adds sample data if there are no jobs, outreach messages or questions
python -m app.seed --reset   # removes the sample rows, then seeds again
python -m app.seed --clear   # removes the sample rows without seeding
```

The sample data is for UI work: 33 jobs across every platform / apply type / status combination,
7 outreach messages and 3 unanswered questions.

- Sample jobs are the ones whose company starts with `[Sample] ` (case-sensitive). Every sample
  outreach message and unanswered question is linked to a sample job.
- `--reset` and `--clear` delete only sample rows: outreach messages and questions linked to
  sample jobs, then the sample jobs. Older versions of the seed created three outreach messages
  without a job; those are recognised by their exact sample recipient profile URLs
  (`https://www.linkedin.com/in/sample-<name>`) with no job.
- Seeding (with or without `--reset`) refuses to run, with exit code 1, if the database has any job
  that isn't sample data. `--clear` still works then.
- Seeding never touches the profile fields, the Template Library or the mirror file.

## Test

```bash
pytest
```

Tests use a temporary SQLite database, a temporary mirror file path, a fake resume, a fake browser,
a fake SMTP server and synthetic Chrome cookie databases, so they never read or modify real data,
start a browser, send email, touch the real browser profiles or read the real Chrome profile /
Keychain.

## Configuration

All settings come from environment variables. A `backend/.env` file is read too if present
(real environment variables take precedence). Relative paths resolve against `backend/`, not the
current working directory.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/jobapply.db` (= `backend/data/jobapply.db`) | SQLAlchemy database URL. The SQLite directory is created on startup. |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated list of origins allowed to call the API. |
| `PROFILE_PATH` | `../profile/CANDIDATE_PROFILE.md` | Generated, read-only mirror of the profile and Template Library. Rewritten after every profile or template change; the parent directory is created if missing. |
| `RESUME_PATH` | `../profile/resume/Harsh_Vaghela.pdf` | Resume PDF served by `GET /api/profile/resume`. When it's missing, the profile summary's `resume` is `null`. |
| `EASY_APPLY_DAILY_CAP` | `25` | LinkedIn Easy Apply submissions per local day. |
| `NAUKRI_DAILY_CAP` | `50` | Naukri one-click applies per local day. |
| `CONNECTION_NOTES_MONTHLY_CAP` | `250` | LinkedIn connection requests with a note per calendar month. |
| `INMAIL_MONTHLY_CAP` | `5` | LinkedIn InMails per calendar month. |
| `EMAIL_DAILY_CAP` | `20` | Automatic emails to hiring posts per local day. |
| `SMTP_HOST` | `smtp.gmail.com` | Mail server for those emails (STARTTLS). |
| `SMTP_PORT` | `587` | Its port. |
| `SMTP_USER` | (empty) | The Gmail address the emails come from; test emails go to it. |
| `SMTP_FROM_NAME` | (empty) | The sender name shown with it. |
| `SMTP_APP_PASSWORD` | (empty) | A Google app password (spaces ignored). Secret: only in `backend/.env`, never logged or returned. Empty = no emails are sent. |
| `BROWSER_PROFILES_DIR` | `../.browser-profiles` | One Chrome profile per platform with the saved login (gitignored). Disconnect deletes a platform's folder. |
| `BROWSER_CHANNEL` | `chrome` | `chrome` = the installed Google Chrome; `chromium` = Playwright's Chromium (needs `playwright install chromium`). |
| `CHROME_USER_DATA_DIR` | `~/Library/Application Support/Google/Chrome` | The user's own Chrome data folder that Connect imports the login from (every `Default` / `Profile N`). |
| `SCAN_ENABLED` | `true` | Run the scanners in the background. `POST /api/scans/run` works either way. |
| `SCAN_INTERVAL_MINUTES` | `30` | Minutes between the end of one run of a group (LinkedIn, Naukri) and the start of its next, until set in the app. |
| `SCAN_HEADLESS` | `true` | Scans and login checks run in an invisible browser. Login windows are always visible. |
| `SCAN_MAX_PAGE_LOADS` | `20` | Page loads per scanner per run (the LinkedIn job search: at most 4 rendered pages and 12 job-data requests). |
| `LINKEDIN_SEARCH_INTERVAL_MINUTES` | `60` | The logged-in LinkedIn job and post searches run at most this often (until set in the app); runs in between record `skipped` with the next time. The old name `LINKEDIN_SCAN_INTERVAL_MINUTES` still works. |
| `LOGIN_TIMEOUT_MINUTES` | `10` | How long a login window waits for the user. |

"Today" and "this month" use the server's local timezone. All API timestamps are UTC
(`2026-10-13T06:45:00Z`).

## Layout

```
app/
  main.py               app factory, CORS, routers under /api, startup (tables, login checks, schedule)
  config.py             settings (pydantic-settings)
  db.py                 engine + table creation
  models.py             enums and tables: Job, OutreachMessage, UnansweredQuestion, Template,
                        ProfileField
  schemas.py            request/response models
  api/                  dashboard, jobs, outreach, templates, questions, profile routers
  services/profile.py   profile response (summary + sections), field upsert/delete
  services/templates.py Template Library ordering and creation
  services/mirror.py    regenerates the profile mirror file (PROFILE_PATH) from the database
  services/connections.py  login state machine: import from Chrome, login window, checks
  browser/              driver interface, Playwright driver, worker threads, Chrome cookie reader
  scanner/              matching rules, pacing, storing results
  scheduler/            scan runs (runner.py) and the background schedule (loop.py)
  services/outreach.py  placeholders, template choice and counts shared by emails and notes
  services/mailer.py    Gmail SMTP (STARTTLS) with the resume attached
  scanner/email_send.py automatic emails to hiring posts (email_send)
  sources/linkedin/     selectors, parsers, login spec, logged-in job search, Easy Apply (engine,
                        answers, skip rules), hiring-post scanner, connection notes (outreach.py)
  sources/naukri/       selectors, parsers, login spec, job scanner
  seed.py               sample data (python -m app.seed [--reset | --clear])
tests/
```

## Deploying separately

The backend and frontend are deployed independently:

- Install from `requirements.txt` and run `uvicorn app.main:app --host 0.0.0.0 --port <port>` from
  this directory (drop `--reload`).
- Set `CORS_ORIGINS` to the frontend's public URL, and point the frontend's `VITE_API_BASE_URL`
  at this server.
- Set `PROFILE_PATH` to where the generated mirror file should go (or anywhere writable if you
  don't need it; a failed mirror write is only logged), and `RESUME_PATH` to the resume PDF.
- Use a persistent location for the SQLite file (`DATABASE_URL`), or point it at another database
  (after installing its driver).
- The logins and scans need a local Chrome and the candidate's browser profiles, so the full app
  is intended to run on the candidate's own machine.
