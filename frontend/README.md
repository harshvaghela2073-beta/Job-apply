# Job Apply — frontend

React + TypeScript + Vite UI for the job-application automation tool. It talks to the backend
only over its REST API (`/api/...`), so it builds and deploys on its own.

## Requirements

- Node 22, npm 10

## Setup

```bash
npm install
cp .env.example .env    # optional; edit VITE_API_BASE_URL if the backend isn't on localhost:8000
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_BASE_URL` | `http://localhost:8000` | Base URL of the backend API, without `/api` and without a trailing slash. |

Vite reads `VITE_*` variables **at build time** and bakes them into the bundle. Changing the value
means rebuilding.

## Commands

```bash
npm run dev       # dev server with hot reload at http://localhost:5173
npm run build     # type-check (tsc -b) and build the static site into dist/
npm run preview   # serve the built dist/ locally to check it
npm run lint      # oxlint
```

The backend must be running for the pages to show data. If it isn't, each page shows
"Can't reach the backend at <url> — is it running?" and the top bar shows a red "Backend offline" pill.

Login state (`GET /api/connections`) is polled every 3 s while a login window is open, every 10 s
while the backend is unreachable, and every 60 s otherwise. The Dashboard polls
`GET /api/scans/status` every 10 s while a scan runs and every 60 s otherwise.

## Deploying

The build output (`dist/`) is a plain static site and can go on any static host (Netlify, Vercel,
Cloudflare Pages, S3 + CloudFront, nginx, …), separately from the backend.

1. Set `VITE_API_BASE_URL` to the deployed backend URL at build time, e.g.
   `VITE_API_BASE_URL=https://api.example.com npm run build`.
2. Upload `dist/`.
3. Configure the host to serve `index.html` for unknown paths (SPA fallback), because the app uses
   client-side routes such as `/linkedin/google-form`.
4. Allow the frontend's origin in the backend's `CORS_ORIGINS` setting, or the browser will block
   the API calls.

## Structure

```
src/
  api/types.ts        enums and models, mirroring the backend API contract
  api/client.ts       fetch wrapper (ApiError with `detail`) and one function per endpoint
  hooks/              useQuery (loading/error/data), useAction (mutations), usePolling, usePopover,
                      useScanStatus, shared counts / profile / connections contexts (each fetched
                      once by its provider)
  components/         layout, LinkedIn/Naukri login pills + popovers and the "log in" banner
                      (top), profile avatar + popover, Scanner panel and collapsible sections
                      (Dashboard), the shared jobs table with its Applied / Pending toggle, tabs,
                      cards, badges, meters, copy button, loading/empty/error states
  pages/              Dashboard, job lists (every LinkedIn / Naukri tab), Mail, Template Library,
                      Analysis, Profile
  lib/format.ts       date formatting (local time, relative) and display labels
  lib/jobs.ts         job-table columns, Applied / Pending, apply links, apply types per platform
  lib/profile.ts      initials, URL / phone display helpers
  lib/templates.ts    template form helpers and search
  lib/clipboard.ts    copy to clipboard (with a fallback for non-secure origins)
  index.css           all styles; White/Black themes (follows the OS until one is picked)
```

## Routes

| Path | Page |
|---|---|
| `/` | Dashboard |
| `/linkedin/easy-apply` | LinkedIn Easy Apply jobs (`/linkedin` redirects here) |
| `/linkedin/google-form` | Jobs with a Google Form |
| `/linkedin/company-website` | LinkedIn jobs to apply to on the company site |
| `/linkedin/mail` | Hiring posts that ask for an email, and the sent messages (`/linkedin/outreach` redirects here) |
| `/naukri/one-click` | Naukri one-click jobs (`/naukri` redirects here) |
| `/naukri/company-website` | Naukri jobs to apply to on the company site |
| `/templates` | Template Library (answers, LinkedIn messages, emails) and unanswered questions |
| `/analysis` | Found / applied / pending per platform and apply type (today or all time), totals and caps |
| `/profile` | Full profile (also shown in the avatar popover at the top right) |
