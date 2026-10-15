# Job-apply backend for Render (docs/DEPLOYMENT.md): FastAPI + the scanners, with Playwright's Chromium.
# Everything the app keeps (database, browser logins, resume, logs, screenshots) lives on the
# persistent disk mounted at /data.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Kolkata

WORKDIR /app

COPY requirements.txt .
# Chromium and the system libraries it needs (Google Chrome isn't available on the server).
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

# The server's settings: Chromium instead of Chrome, and every file on the persistent disk.
ENV BROWSER_CHANNEL=chromium \
    SCAN_HEADLESS=true \
    DATABASE_URL=sqlite:////data/jobapply.db \
    BROWSER_PROFILES_DIR=/data/browser-profiles \
    RESUME_PATH=/data/resume.pdf \
    PROFILE_PATH=/data/CANDIDATE_PROFILE.md \
    LOG_FILE=/data/logs/backend.log \
    DIAGNOSTICS_DIR=/data/diagnostics

EXPOSE 10000

# One worker only: the scan schedule runs inside the app, so two workers would scan (and apply,
# and email) twice. Render passes the port in $PORT.
CMD ["sh", "-c", "mkdir -p /data/logs /data/diagnostics /data/browser-profiles && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
