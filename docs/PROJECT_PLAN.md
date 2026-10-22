# Job Apply Automation — Project Plan

Status: **draft, awaiting candidate details** (2026-10-13)

## 1. Goal

A personal tool that finds relevant jobs on LinkedIn and Naukri, applies automatically where the
platform allows a one-step apply, and gathers everything else (company-site applications,
Google Forms, hiring-manager emails, recruiter outreach) in one UI for the candidate to finish.

## 2. Requirements (from the kickoff prompt)

### Frontend (React)

- **Dashboard**: jobs applied to, with applied time, job title, company, and a short description.
- **Apply on company website**: jobs that need a manual application, with links.
- **Google Form**: jobs that use a Google Form, with the form link and job details.
  The candidate fills these by hand for now; auto-fill comes later.
- **Profile**: an avatar at the top right opens a card with the candidate's details: name, contact,
  LinkedIn and Naukri links, current job, salary, skills and resume. The `/profile` page shows every
  field. The data lives in the database (`ProfileField`), and new fields appear without code changes.
- **Template Library**: a page where the candidate views, copies, adds and edits templates. There are
  three kinds: application answers, LinkedIn messages and emails. They are stored in the database
  (`Template`), and `profile/CANDIDATE_PROFILE.md` is a generated copy. Questions the tool met but
  couldn't answer show up here to be filled in.
- **Navigation**
  - Top bar: `Dashboard` | `LinkedIn` | `Naukri` | `Template Library`, plus a profile avatar at the top right
  - Dashboard: all applications across platforms, newest first (applied time, title, company,
    platform, short description)
  - LinkedIn sub-tabs: `Easy Apply` | `Google Form` | `Apply on Company Website` | `Outreach & Emails`
  - Naukri sub-tabs: `One-click Apply` | `Apply on Company Website`
  - `Outreach & Emails` is the review queue for hiring-manager emails, connection notes and InMails.

### Backend (Python)

- **Naukri**: search openings that match the candidate's skills. Apply directly to one-click jobs
  and send company-site jobs to the UI.
- **LinkedIn Jobs**: apply directly to Easy Apply jobs. Send company-site jobs to the UI.
- **LinkedIn recruiter outreach** (Premium): about 250 connection requests with a note per month,
  and 5 or more personal messages (InMail) per month.
- **LinkedIn posts** (highest priority):
  - Posts with an **email**: send the hiring person a well-formatted email with the candidate's
    details so they can get in touch.
  - Posts with a **Google Form** (top priority): show the form link and post details in the UI.
- **Indeed**: mentioned as a source, but it has no tab in the UI yet (see open questions).

## 3. Proposed architecture

```
Job-apply/
├── backend/                  Python 3.13, FastAPI
│   ├── app/api/              REST endpoints the frontend calls
│   ├── app/models.py, db.py  SQLite + SQLModel (jobs, outreach queue, unanswered questions)
│   ├── app/services/         profile_md.py edits the Template Library table in the profile
│   ├── app/sources/
│   │   ├── linkedin/         job search, Easy Apply, post scanner, recruiter outreach
│   │   └── naukri/           job search, one-click apply
│   ├── app/answers/          maps form questions to answers in profile/CANDIDATE_PROFILE.md
│   ├── app/mailer/           sends hiring-manager emails (Gmail API)
│   └── app/scheduler/        periodic scans with daily and monthly caps (APScheduler)
├── frontend/                 React + Vite + TypeScript
├── profile/CANDIDATE_PROFILE.md
├── prompts/PROMPTS.md
└── docs/
```

**How the platforms are accessed.** LinkedIn and Naukri have no public API for job seekers, so the
backend uses **Playwright** with a persistent browser profile on the candidate's Mac. The candidate
logs in once by hand, which also covers OTP and 2FA. The app never stores platform passwords.

**Core data model (draft)**

- `Job`: source, external_id, title, company, location, url, short_description,
  apply_type (`easy_apply` | `one_click` | `company_site` | `google_form` | `email`),
  status (`found` | `queued` | `applied` | `needs_manual` | `failed` | `skipped`),
  applied_at, form_url, contact_email, post_url, found_at
- `OutreachMessage`: the single approval queue for hiring-manager emails, connection notes and InMails.
  Fields: kind, recipient, subject, body, status (`draft` | `approved` | `sent` | `rejected` | `failed`),
  approved_at, sent_at
- `UnansweredQuestion`: question text, where it was seen, and the candidate's answer.
  Answered questions are copied into the profile.

Exact fields and endpoints: [API_CONTRACT.md](API_CONTRACT.md).

## 4. Risks

- **Platform terms.** The LinkedIn User Agreement forbids bots and automated activity, including
  automated applying and messaging. Accounts that break the rules can be restricted, and Premium
  does not protect against this. Naukri has similar terms. To reduce the risk:
  - run locally, in the candidate's real browser profile
  - pace actions like a person would, within daily caps
  - stop as soon as a CAPTCHA or warning appears
  - put anything that messages a person through a review queue (pending decision)
- **Selectors break.** Site markup changes. Each source is isolated so one break doesn't stop the others.
- **Unknown form questions.** These are never guessed. They go to `UnansweredQuestion`, and the job
  waits for the candidate.

## 5. Phases

1. Scaffold the backend, frontend, and database. Build the dashboard, the tab layout (with sample
   data) and the Template Library.
2. Naukri: search, one-click apply, company-site list.
3. LinkedIn Jobs: search, Easy Apply, company-site list.
4. LinkedIn posts: scan for emails and Google Form links. Google Form tab. Email drafts and sending.
5. LinkedIn recruiter outreach with monthly caps (250 notes, 5 or more InMails).
6. Later: Google Form auto-fill, and Indeed if wanted.

## 6. Agent team split (for parallel work)

Each teammate owns its own directory so teammates don't overwrite each other's files:

| Teammate | Owns |
|---|---|
| Backend core | `backend/app/api`, `backend/app/models.py`, `backend/app/db.py`, `backend/app/services`, `backend/app/scheduler` |
| LinkedIn source | `backend/app/sources/linkedin` |
| Naukri source | `backend/app/sources/naukri` |
| Frontend | `frontend/` |

The shared API contract (data models and endpoint shapes) is written first by Backend core, and
the other teammates build against it.

## 7. Open questions

- Whether to add an LLM (Claude API) later to tailor emails and outreach notes per job.
  For now everything is template-based.

## 8. Decisions

| Date | Decision |
|---|---|
| 2026-10-13 | Stack: Python backend, React frontend (given by the candidate). |
| 2026-10-13 | Automation level: Easy Apply and Naukri one-click submit automatically within daily caps. Hiring-manager emails, connection notes and InMails wait in the `Outreach & Emails` queue until the candidate approves them. |
| 2026-10-13 | Answers come from a **Template Library** UI section backed by `profile/CANDIDATE_PROFILE.md`. No LLM for now. Unknown questions are flagged, never guessed. |
| 2026-10-13 | Indeed is deferred to a later phase. LinkedIn and Naukri come first. |
| 2026-10-13 | UI layout: top bar `Dashboard` / `LinkedIn` / `Naukri` / `Template Library`. LinkedIn gets an extra `Outreach & Emails` sub-tab. |
| 2026-10-13 | Database: SQLite through SQLModel. Switching to Postgres later is a `DATABASE_URL` change. |
| 2026-10-13 | `backend/` and `frontend/` are separate, independently deployable apps. Each has its own dependencies, README and `.env.example`, and all config comes from environment variables. |
| 2026-10-13 | UI: full-width layout, monochrome White and Black themes with a switch in the top bar, and a read-only Profile tab. |
| 2026-10-13 | The candidate profile and all templates (answers, LinkedIn messages, emails) are stored in the database. `profile/CANDIDATE_PROFILE.md` becomes a generated copy. Only details the candidate provided are stored. The profile moves to an avatar menu at the top right. Sample data is removed. |
| 2026-10-13 | Job-search priority (stored as profile fields, shown in the Template Library). Experience is the outer loop: 1+, then 2+, then 3+ years. Roles in order: Software Development Engineer, SDE1, SDE2, Java Developer, Backend Developer, Golang Developer, Full Stack Developer, Forward Deployed Engineer. Locations: Bengaluru, Remote. |
| 2026-10-13 | Experience is "1.9+ years", meaning 1 year 9 months: a 6-month internship from Jan 2025 plus full-time SDE since July 2025. All templates use 1.9+. The expected CTC in the email is the 16-20 LPA range. The email subject is "[Job title] Opportunity". LinkedIn messages are kept under 300 characters so they fit a connection note. |
| 2026-10-13 | **One-day discovery trial** (the candidate's plan): the candidate logs in to LinkedIn and Naukri from the app (the Connect buttons and status dots at the top right). The backend then scans every 30 minutes, all day, for jobs posted today and LinkedIn hiring posts with a Google Form or an email. Nothing is applied or sent; the candidate decides the next step after the trial. Platform checks on 2026-10-13: Naukri blocks headless Chrome unless the browser identifies as normal Chrome. LinkedIn's public job search, including the Easy Apply and remote filters, works without a login, but post search needs one. |
| 2026-10-13 | **Naukri one-click auto-apply is on** (the candidate's request): after each scan, it applies to at most 10 of today's one-click jobs per run and 50 a day, in priority order. Skip rules: salary below the current CTC, none of the candidate's skills, fresher or internship postings, and consultancies or individual recruiters. Recruiter questions are never guessed. LinkedIn applications, emails and messages stay manual. First live application: Aviato Consulting, Forward Deployed Software Engineer. |
| 2026-10-13 | **LinkedIn:** logged-out search is blocked from the candidate's connection (HTTP 429, then a login wall), so job search uses the saved LinkedIn login, hourly. **Easy Apply auto-apply** gets its own switch (off until a one-job test passes): 5 per run, 25 a day, the same skip rules. Forms are filled only from the profile and templates; unknown required fields mean the draft is discarded and the job is left for the candidate. Form numbers: expected CTC 18 LPA; experience 1.9, or 2 when only whole numbers are accepted. |
| 2026-10-14 | The candidate restated the LinkedIn requirements: (1) Google Forms from hiring posts, (2) reach out to hiring or talent-acquisition people, (3) email the hiring person when a post asks for it, (4) Easy Apply, (5) a list of company-portal jobs. **Emails are sent through Gmail with an app password** kept in `backend/.env`, from the candidate's Gmail, with the resume attached. **Emails and LinkedIn connection notes and InMails are sent automatically**, within limits (250 notes and 5 InMails a month), instead of waiting in the approval queue. This changes the 2026-10-13 decision. Each channel gets one checked test send before it runs unattended. |
| 2026-10-14 | **Easy Apply and Naukri recruiter questions are filled from the candidate's details**, and answers worked out from the profile (e.g. years with a skill, relocation to Bengaluru, notice period) are saved to the Template Library for next time. Questions with no answer are still never guessed. |
| 2026-10-14 | **Naukri is paused** (new `pause_naukri` setting) while LinkedIn is fixed; LinkedIn scanners now run first in every round. |
| 2026-10-14 | **Chrome runs as a native Apple-silicon app.** The backend's Python is an Intel build (Homebrew in `/usr/local`) running under Rosetta on the M1 Pro, so Playwright started Chrome under Rosetta too; LinkedIn's pages then kept a CPU core at 100% and every click timed out. The driver now starts Chrome through `app/browser/chrome-native.sh` (`arch -arm64`) when it detects Rosetta: the Easy Apply form went from timing out to 5 steps in about 20 seconds. First live LinkedIn Easy Apply application: Tata Consultancy Services, Java Software Engineer. |
| 2026-10-15 | **More Easy Apply jobs:** every LinkedIn job search runs first with LinkedIn's "Easy Apply" filter, then without it (company-site jobs), up to 12 result pages per run. Hiring-post emails held back by the daily cap are sent the next day (posts found today or yesterday). "Run now" in the app also runs the LinkedIn searches when the last one was at least 15 minutes ago. |
| 2026-10-15 | **Google Form posts:** the candidate asked why no forms showed up. The post scan now runs 5 form searches first, follows `lnkd.in` links with a plain request, and leaves out course/training adverts. Live check: 67 posts read, 30 with an email, 1 with a form (a training advert). Hiring posts with a Google Form for these roles are rare. |
| 2026-10-15 | **Google Form posts from the past 7 days** are listed (the candidate's choice); the form searches look back a week. Email posts, Easy Apply and Naukri stay "posted today" only. |
| 2026-10-15 | **UI redesign** (the candidate's list): an engaging title; the scanner panel collapsed behind an arrow, with a LinkedIn and a Naukri group and a pause switch per platform and per feature; the Dashboard shows All jobs (newest found first) with an Applied/Pending column; the summary moves to a new **Analysis** tab (LinkedIn and Naukri: found / applied / pending per apply type, emails sent, Google Forms found); job lists sorted by posted time, without the matched-role column, with two states Applied and Pending; Template Library sections collapsible, search priority editable, no dates or # numbering; a minimal Profile; no explanation text under features. |
| 2026-10-15 | **LinkedIn and Naukri scan separately** (the candidate's request): each has its own interval, Run now and status in its own section of the scanner panel; the two may run at the same time. Interval changes need a Save confirmation. Analysis shows Applied/Pending as rings. |
| 2026-10-15 | **Deployment: frontend on Vercel, backend on Render** (the candidate's choice, after being told about the data-centre IP risk for LinkedIn/Naukri). Added: an app password (bearer token), pushing the LinkedIn/Naukri login from the Mac's Chrome to the server, uploading the resume and the database, a Docker image with Chromium, a Render blueprint with a persistent disk, and a Vercel rewrite. |
| 2026-10-15 | **Runs locally only** (the candidate dropped the Vercel/Render plan; the files stay). **Accuracy for interview calls:** skip off-stack titles, senior titles, fewer than 2 of the candidate's skills, and a company skip list (aggregators/staffing: Jobgether, Crossing Hurdles, Scoutit, Applicantz, CodeRound AI); no per-company limit (declined). LinkedIn connection notes fixed for the new profile page; the 60 notes the bug blocked are retried once. |
| 2026-10-21 | The candidate (Naukri Pro, no calls yet) restated targets: Bengaluru only, 16-18 LPA; 1+ years as SDE / Software Development Engineer / Backend Engineer / Java Developer / Full Stack Engineer first, then 2+ years with SDE2 / SDE1; skills add React, GCP, CI/CD. Remote, Golang Developer and Forward Deployed Engineer dropped. **Easy Apply never waits:** unanswered questions get set defaults (Yes to availability/consent, self-ratings 4/5 own skills, 2/5 others), free text with no answer skips the job. LinkedIn feed scanning for hiring posts, prompt company-site links, Naukri filters + recommended jobs, a remove (×) button on job rows. |
