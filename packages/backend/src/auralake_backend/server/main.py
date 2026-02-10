"""FastAPI application entry-point for the auralake server.

Start the server via the ``auralake-server`` console script (defined in
``pyproject.toml``) or by calling :func:`run` directly.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from auralake_shared.core.logging import setup_logging
from auralake_shared.providers import get_provider
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from auralake_backend.core.config_loader import is_configured, load_config_from_db
from auralake_backend.db.engine import get_engine, init_engine
from auralake_backend.db.models import ApiKey
from auralake_backend.server.agent.router import router as agent_router
from auralake_backend.server.auth import create_api_key
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
from auralake_backend.server.settings.router import router as settings_router
from auralake_backend.server.spot.router import router as spot_router
from auralake_backend.server.tags.router import router as tags_router

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan: init DB, load config from DB, start provider."""
    import os

    setup_logging()

    # Database — schema is managed by alembic (db-migrate init container)
    db_url = os.environ.get("AURALAKE_DATABASE_URL", "postgresql+psycopg://localhost:5432/auralake")
    init_engine(db_url)
    logger.info("database_initialized")

    # Validate encryption key early — fail fast instead of crashing on first
    # connection create.
    encryption_key = os.environ.get("AURALAKE_ENCRYPTION_KEY", "")
    if not encryption_key:
        logger.error(
            "encryption_key_missing",
            hint=(
                "Set AURALAKE_ENCRYPTION_KEY in your environment. "
                'Generate one with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ),
        )
        import sys

        sys.exit(1)

    # Validate key format by attempting to create a Fernet instance
    try:
        from auralake_backend.core.encryption import _get_fernet

        _get_fernet()
    except Exception as exc:
        logger.error("encryption_key_invalid", error=str(exc))
        import sys

        sys.exit(1)

    # Auto-bootstrap: create first API key if none exist
    with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
        existing = session.exec(select(ApiKey).limit(1)).first()
        if not existing:
            record, raw_key = create_api_key(session, "auto-bootstrap")
            logger.info(
                "auto_bootstrap_completed",
                api_key=raw_key,
                key_prefix=record.key_prefix,
                hint="Save this key — it will not be shown again.",
            )
        else:
            logger.debug("api_keys_exist_skipping_bootstrap")

    # Load config from DB connections + env overrides
    with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
        config = load_config_from_db(session)
        app.state.config = config
        app.state.configured = is_configured(session)

    # Provider
    if app.state.configured:
        try:
            provider = get_provider(config.provider, config)
            app.state.provider = provider
            logger.info("provider_initialized", provider=config.provider)
        except Exception:
            logger.warning("provider_init_failed", provider=config.provider)
            app.state.provider = None
    else:
        app.state.provider = None
        logger.info("server_started_unconfigured")

    logger.info("server_started")

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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- Exception handlers -------------------------------------------------
    register_exception_handlers(app)

    # -- Health check -------------------------------------------------------
    @app.get("/health", tags=["health"])
    async def health() -> dict[str, object]:
        configured = getattr(app.state, "configured", False) and getattr(
            app.state, "provider", None
        ) is not None
        return {"status": "ok", "configured": configured}

    # -- Settings & auth routers (no require_configured gate) ---------------
    app.include_router(settings_router, prefix="/api/v1", tags=["settings"])

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
    """Launch the server with uvicorn."""
    uvicorn.run(
        "auralake_backend.server.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
