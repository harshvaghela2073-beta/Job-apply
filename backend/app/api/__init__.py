from fastapi import APIRouter

from app.api import (
    admin,
    analytics,
    apply,
    auth,
    connections,
    dashboard,
    jobs,
    outreach,
    profile,
    questions,
    scans,
    settings,
    templates,
)

api_router = APIRouter(prefix="/api")


@api_router.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


api_router.include_router(dashboard.router)
api_router.include_router(analytics.router)
api_router.include_router(jobs.router)
api_router.include_router(apply.router)
api_router.include_router(settings.router)
api_router.include_router(outreach.router)
api_router.include_router(templates.router)
api_router.include_router(questions.router)
api_router.include_router(profile.router)
api_router.include_router(connections.router)
api_router.include_router(scans.router)
api_router.include_router(auth.router)
api_router.include_router(admin.router)
