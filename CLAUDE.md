# Job-apply

Personal job-application automation: Python (FastAPI) backend and React frontend. It finds jobs on
LinkedIn and Naukri, auto-applies where the platform allows, and lists everything else
(company-site applications, Google Forms, hiring-manager emails, recruiter outreach) in the UI.

Full spec, architecture, phases and decisions: [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md).

## Commands

`backend/` and `frontend/` are separate apps that can be deployed on their own. Never import across them.
`docs/API_CONTRACT.md` is their only interface: change it first, then both sides.

```bash
# Backend: Python 3.13, venv in backend/.venv
cd backend && .venv/bin/pytest                         # tests
# Sample data is only for a separate dev DB: seeding refuses if the DB holds real jobs.
DATABASE_URL=sqlite:///data/dev.db .venv/bin/python -m app.seed   # (run from backend/)
cd backend && .venv/bin/python -m app.seed --clear     # remove "[Sample]" rows only
cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

# Frontend: Vite + React + TypeScript
cd frontend && npm run build && npm run lint           # type-check, build, lint
cd frontend && npm run dev                             # http://localhost:5173
```

## Key files

- **Candidate profile and templates live in the database**, in the `ProfileField` and `Template`
  tables (`backend/data/jobapply.db`, gitignored). Everything the tool says or fills in on the
  candidate's behalf comes from here.
  - To add or change data, call the running backend:
    - `PUT /api/profile/fields` for profile fields
    - `POST /api/templates` or `PUT /api/templates/{id}` for templates
  - Store only what the user actually gave. Don't invent fields, placeholder answers or `TBD` rows.
    Fix obvious spelling typos in data fields and tell the user.
  - Flag inconsistencies between values, for example different experience figures in different
    templates.
- `profile/CANDIDATE_PROFILE.md`: a **generated, read-only copy** of the database, rewritten on every
  change. Never edit it by hand; read it to see the current data.
- `profile/resume/Harsh_Vaghela.pdf`: the resume, set by `RESUME_PATH`.
- `prompts/PROMPTS.md`: log of every user prompt.
  - It is appended automatically by the UserPromptSubmit hook (`.claude/hooks/log-prompt.sh`).
  - Don't rewrite past entries.
- `docs/PROJECT_PLAN.md`: record each decision the user makes in its Decisions table.

## Rules

- Never store LinkedIn, Naukri, or email passwords in code, config, or git.
  - Platform access uses a persistent Playwright browser profile per platform in
    `.browser-profiles/` (gitignored).
  - The user connects from the app. The top-right **Connect** button opens the login in the user's
    own Chrome. The backend then copies that site's cookies from Chrome
    (`POST /api/connections/{platform}/import`); macOS asks for "Chrome Safe Storage" access the
    first time.
  - Never read the user's real Chrome profile, the Keychain or `.browser-profiles/` yourself, in
    scripts or in tests. That is credential data, read only by the app when the user clicks
    Connect. Tests use synthetic cookie databases.
- The backend runs the scanners every `SCAN_INTERVAL_MINUTES` (30) while `SCAN_ENABLED` is true,
  in this order:
  - `linkedin_jobs`: the saved LinkedIn login, hourly
  - `linkedin_apply`: Easy Apply, with its own switch
  - `linkedin_posts`: hiring posts with Google Forms or emails
  - `email_send`, `linkedin_outreach`: emails and connection notes, each with its own switch
  - `naukri_jobs`, `naukri_apply`: Naukri one-click, with skip rules and recruiter questions answered
    from the profile; both skipped while the `pause_naukri` setting is on
  - Auto-apply is controlled by `GET`/`PUT /api/settings`. Form answers come only from the profile and
    templates; required fields with no answer mean the draft is discarded and the job is left for
    the user.
  - The user's backend runs with `--reload`, so every save to a backend file restarts it, and that
    closes any open login window and interrupts a running scan.
  - Failed login checks save a screenshot to `backend/data/diagnostics/`.
- Every automated action on LinkedIn or Naukri must respect the daily and monthly caps in config.
  - It must stop and surface the problem when a CAPTCHA, a login wall, or a warning appears.
- Keep each source (`backend/app/sources/<platform>`) self-contained, so a selector break in one
  platform doesn't affect the others.

## Deployment

- Frontend on Vercel (`frontend/vercel.json`), backend on Render (`render.yaml`, `backend/Dockerfile`:
  Chromium, every file on the persistent disk at `/data`, one worker). Steps: `docs/DEPLOYMENT.md`.
- `APP_PASSWORD` (empty locally) turns on the app login; every `/api` call then needs the token.
- The server has no Chrome: logins, the resume and the database are sent from the Mac with
  `python -m app.tools.push --server <url> {login|resume|database}`. Only the user runs `login` (it
  reads their Chrome and the Keychain).
- Personal files (`profile/`, `prompts/PROMPTS.md`, `.env`, the database, `.browser-profiles/`)
  are gitignored; tests and docs use made-up contact details.

## Agent teams

Agent teams are enabled for this project via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`.

- Each teammate owns one area: backend core, LinkedIn source, Naukri source, or frontend.
  The ownership table is in `docs/PROJECT_PLAN.md` §6.
- Don't edit files outside your area. If you need a change there, message the teammate who owns it.
- Backend core owns the API contract and data models. Change them there first, then tell the others.
