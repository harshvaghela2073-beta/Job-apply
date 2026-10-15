# Job Apply Automation

A personal tool that finds jobs on LinkedIn and Naukri, applies automatically where the platform
allows it, and collects everything else (company-site applications, Google Forms, hiring-manager
emails, recruiter outreach) in one dashboard.

| Folder | What it is |
|---|---|
| [backend/](backend/) | Python FastAPI API with a SQLite database. It can be deployed on its own; see its README. |
| [frontend/](frontend/) | React dashboard built with Vite and TypeScript. It can be deployed on its own; see its README. |
| [profile/CANDIDATE_PROFILE.md](profile/CANDIDATE_PROFILE.md) | The candidate's details and template answers. The Template Library page edits this file. |
| [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | Requirements, architecture, phases and decisions. |
| [docs/API_CONTRACT.md](docs/API_CONTRACT.md) | The API between the backend and the frontend. |
| [prompts/PROMPTS.md](prompts/PROMPTS.md) | Log of every prompt given to Claude Code in this project. |

## Run locally

Use two terminals. Exact setup steps are in each folder's README.

```bash
# Terminal 1: backend on http://localhost:8000
cd backend && source .venv/bin/activate && uvicorn app.main:app --reload --port 8000

# Terminal 2: frontend on http://localhost:5173
cd frontend && npm run dev
```
