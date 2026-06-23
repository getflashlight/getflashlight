"""FastAPI application factory and ``auralake-server`` entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auralake import __version__
from auralake.core.logging import get_logger, setup_logging
from auralake.core.settings import get_settings
from auralake.server.routers import health, ingest, metrics
from auralake.store.engine import init_engine

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    setup_logging()
    if get_settings().auto_migrate:
        # Fallback when no dedicated migrate service/init-container ran. No-op if
        # already at head. Set AURALAKE_AUTO_MIGRATE=false to disable.
        from auralake.store.migrate import upgrade_to_head

        upgrade_to_head()
    init_engine()
    logger.info("server_start", version=__version__)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Auralake", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(ingest.router)
    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.server_host, port=settings.server_port)


if __name__ == "__main__":
    run()
