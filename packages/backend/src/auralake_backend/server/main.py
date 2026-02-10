"""FastAPI application entry-point for the auralake server.

Start the server via the ``auralake-server`` console script (defined in
``pyproject.toml``) or by calling :func:`run` directly.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from auralake_shared.core.config import load_config
from auralake_shared.core.logging import setup_logging
from auralake_shared.models.config import AuraLakeConfig
from auralake_shared.providers import get_provider
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auralake_backend.db.engine import create_all_tables, init_engine
from auralake_backend.server.agent.router import router as agent_router
from auralake_backend.server.budgets.router import router as budgets_router
from auralake_backend.server.clusters.router import router as clusters_router
from auralake_backend.server.cost.router import router as cost_router
from auralake_backend.server.delta.router import router as delta_router
from auralake_backend.server.errors import register_exception_handlers
from auralake_backend.server.jobs.router import router as jobs_router
from auralake_backend.server.policies.router import router as policies_router
from auralake_backend.server.query.router import router as query_router
from auralake_backend.server.resources.router import router as resources_router
from auralake_backend.server.routing.router import router as routing_router
from auralake_backend.server.spot.router import router as spot_router
from auralake_backend.server.tags.router import router as tags_router

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan: load config, init DB, start provider."""
    setup_logging()

    config: AuraLakeConfig = load_config()
    app.state.config = config

    # Database
    init_engine(config.database.url)
    create_all_tables()
    logger.info("database_initialized", url=config.database.url)

    # Provider
    provider = get_provider(config.provider, config)
    app.state.provider = provider
    logger.info("provider_initialized", provider=config.provider)

    logger.info(
        "server_started",
        host=config.server.host,
        port=config.server.port,
    )

    yield

    logger.info("server_shutdown")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build and return the fully-configured FastAPI application."""
    app = FastAPI(
        title="Auralake",
        summary="Lakehouse cost optimization API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # -- CORS ---------------------------------------------------------------
    # Attempt to read CORS origins from config; fall back to permissive
    # defaults if no configuration file is available yet.
    try:
        config = load_config()
        cors_origins = config.server.cors_origins
    except Exception:
        cors_origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- Exception handlers -------------------------------------------------
    register_exception_handlers(app)

    # -- Health check -------------------------------------------------------
    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # -- Feature routers ----------------------------------------------------
    app.include_router(cost_router, prefix="/api/v1/cost", tags=["cost"])
    app.include_router(clusters_router, prefix="/api/v1/clusters", tags=["clusters"])
    app.include_router(resources_router, prefix="/api/v1/resources", tags=["resources"])
    app.include_router(spot_router, prefix="/api/v1/spot", tags=["spot"])
    app.include_router(delta_router, prefix="/api/v1/delta", tags=["delta"])
    app.include_router(jobs_router, prefix="/api/v1/jobs", tags=["jobs"])
    app.include_router(query_router, prefix="/api/v1/query", tags=["query"])
    app.include_router(policies_router, prefix="/api/v1/policies", tags=["policies"])
    app.include_router(budgets_router, prefix="/api/v1/budgets", tags=["budgets"])
    app.include_router(tags_router, prefix="/api/v1/tags", tags=["tags"])
    app.include_router(routing_router, prefix="/api/v1/routing", tags=["routing"])
    app.include_router(agent_router, prefix="/api/v1/agent", tags=["agent"])

    return app


# The module-level app instance is used by ``uvicorn`` when pointed at
# ``auralake.server.main:app``.
app = create_app()


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------


def run() -> None:
    """Launch the server with uvicorn.

    CORS origins are configured from the loaded configuration at app creation
    time.  The lifespan handler initialises the database and provider.
    """
    # We rely on lifespan to load config, but we still need host/port for
    # uvicorn.run().  Do a quick config load here just for those values.
    try:
        config = load_config()
        host = config.server.host
        port = config.server.port
    except Exception:
        host = "0.0.0.0"
        port = 8000

    uvicorn.run(
        "auralake_backend.server.main:app",
        host=host,
        port=port,
        reload=False,
    )
