"""FastAPI application. Run with ``uvicorn app.main:app`` from the backend/ directory."""

import logging
from collections.abc import AsyncIterator
from logging.handlers import RotatingFileHandler
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from app.api import api_router
from app.api.auth import RequireLogin
from app.browser import BrowserPool
from app.browser.worker import DriverFactory
from app.config import BACKEND_DIR, Settings, get_settings
from app.db import create_db_engine, init_db
from app.models import Platform
from app.scanner.base import Scanner, set_diagnostics_dir
from app.scheduler.loop import Scheduler
from app.scheduler.runner import ScanService
from app.services.app_settings import run_interval
from app.services.auth import AuthService
from app.services.connections import ConnectionService

logger = logging.getLogger(__name__)


def _log_to_console(log_file: str = "") -> None:
    """Show the app's own INFO logs (scan summaries, logins) next to uvicorn's, and keep them in a
    rotating file under data/logs/ so problems can be read afterwards."""
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)
    if log_file and not any(isinstance(h, RotatingFileHandler) for h in app_logger.handlers):
        path = Path(log_file)
        if not path.is_absolute():
            path = BACKEND_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
        app_logger.addHandler(file_handler)


def playwright_factory(settings: Settings) -> DriverFactory:
    def factory():
        from app.browser.playwright_driver import PlaywrightDriver

        return PlaywrightDriver(channel=settings.browser_channel)

    return factory


def create_app(
    settings: Settings | None = None,
    *,
    driver_factory: DriverFactory | None = None,
    scanners: list[Scanner] | None = None,
) -> FastAPI:
    """``driver_factory`` and ``scanners`` are replaced by fakes in tests."""
    settings = settings or get_settings()
    _log_to_console(settings.log_file)
    set_diagnostics_dir(settings.diagnostics_dir)
    engine = create_db_engine(settings.database_url)
    browsers = BrowserPool(
        settings.browser_profiles_dir,
        driver_factory or playwright_factory(settings),
        stall_seconds=settings.browser_stall_minutes * 60,
    )
    connections = ConnectionService(engine, settings, browsers)
    scans = ScanService(engine, settings, browsers, connections, scanners)
    scheduler = Scheduler(
        scans, enabled=settings.scan_enabled, intervals=dict.fromkeys(Platform, settings.scan_interval_minutes)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db(engine)
        connections.startup()
        scans.startup()
        connections.check_all_in_background()
        with Session(engine) as session:  # each group's interval set in the app, if any
            for platform in Platform:
                scheduler.set_interval(platform, run_interval(session, platform, settings.scan_interval_minutes))
        scheduler.start()
        yield
        await scheduler.stop()
        scans.shutdown()
        connections.shutdown()
        browsers.shutdown()
        engine.dispose()

    app = FastAPI(title="Job-apply API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.browsers = browsers
    app.state.connections = connections
    app.state.scans = scans
    app.state.scheduler = scheduler
    app.state.auth = auth = AuthService(settings)

    # The last middleware added is the outermost: CORS wraps the login check, so its 401 responses
    # carry CORS headers too (the frontend can read them and show the password screen).
    app.add_middleware(RequireLogin, auth=auth)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    return app


app = create_app()
