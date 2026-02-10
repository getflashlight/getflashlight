"""Collector agent — continuously captures Spark query plans and metrics.

Started via `auralake agent start`. Runs as a foreground process or daemon.
"""

from __future__ import annotations

import signal
import sys
from datetime import datetime

from auralake_shared.core.logging import get_logger, setup_logging
from auralake_shared.models.config import AuraLakeConfig

from auralake_backend.agent.plan_parser import PlanParser
from auralake_backend.agent.scheduler import Scheduler

logger = get_logger(__name__)


class Collector:
    """Main collector agent that polls Databricks for queries and plans."""

    def __init__(self, config: AuraLakeConfig) -> None:
        self.config = config
        self.parser = PlanParser()
        self.scheduler = Scheduler(config.agent.interval_seconds)
        self._setup_signal_handlers()

    def start(self) -> None:
        """Start the collection loop."""
        setup_logging(verbose=True)
        logger.info(
            "collector_starting",
            interval=self.config.agent.interval_seconds,
            lookback_hours=self.config.agent.query_lookback_hours,
        )

        # Initialize DB
        try:
            from auralake_backend.db.engine import init_engine

            init_engine(self.config.database.url)
        except Exception as exc:
            logger.error("db_init_failed", error=str(exc))
            sys.exit(1)

        self.scheduler.start(self._collect)

    def stop(self) -> None:
        """Stop the collector."""
        logger.info("collector_stopping")
        self.scheduler.stop()

    def _collect(self) -> None:
        """Single collection run."""
        logger.info("collection_run_started", timestamp=datetime.utcnow().isoformat())

        try:
            # Get provider
            from auralake_shared.providers import get_provider

            provider = get_provider(self.config.provider, self.config)
            query_client = provider.get_query_client()

            # Fetch recent queries
            queries = query_client.get_query_history(
                hours=self.config.agent.query_lookback_hours,
                limit=self.config.agent.max_queries_per_run,
            )
            logger.info("queries_fetched", count=len(queries))

            plans_collected = 0
            for query in queries:
                query_id = query.get("query_id", "")
                query_text = query.get("query_text", "")

                if not query_text:
                    continue

                # Get EXPLAIN plan
                try:
                    plan_text = query_client.explain_query(query_text)
                    if plan_text:
                        spark_plan = self.parser.parse(query_id, plan_text, query_text)
                        self._store_plan(query, spark_plan)
                        plans_collected += 1
                except Exception as exc:
                    logger.warning("explain_failed", query_id=query_id, error=str(exc))

            # Update agent state
            self._update_state(len(queries), plans_collected)
            logger.info("collection_run_completed", queries=len(queries), plans=plans_collected)

        except Exception as exc:
            logger.error("collection_run_failed", error=str(exc))

    def _store_plan(self, query: dict, spark_plan) -> None:
        """Store a parsed plan in the database."""
        try:
            from auralake_backend.db.engine import get_session
            from auralake_backend.db.models import QueryPlan

            with get_session() as session:
                plan_record = QueryPlan(
                    workspace_id=query.get("warehouse_id", ""),
                    query_id=query.get("query_id", ""),
                    query_text=spark_plan.query_text,
                    physical_plan=spark_plan.physical_plan,
                    parsed_plan=[n.model_dump() for n in spark_plan.parsed_nodes],
                    anti_patterns=[a.model_dump() for a in spark_plan.anti_patterns],
                    duration_ms=query.get("duration_ms"),
                    rows_scanned=spark_plan.rows_scanned,
                    bytes_read=spark_plan.bytes_read,
                    shuffle_bytes=spark_plan.shuffle_bytes,
                    spill_bytes=spark_plan.spill_bytes,
                    captured_at=datetime.utcnow(),
                )
                session.add(plan_record)
                session.commit()
        except Exception as exc:
            logger.warning("plan_store_failed", query_id=query.get("query_id"), error=str(exc))

    def _update_state(self, queries_count: int, plans_count: int) -> None:
        """Update agent state in the database."""
        try:
            from auralake_backend.db.engine import get_session
            from auralake_backend.db.repositories import AgentStateRepository

            with get_session() as session:
                repo = AgentStateRepository(session)
                state = repo.get_or_create("default")
                state.queries_collected = (state.queries_collected or 0) + queries_count
                state.plans_collected = (state.plans_collected or 0) + plans_count
                state.last_run_at = datetime.utcnow()
                state.status = "running"
                repo.update(state)
        except Exception as exc:
            logger.warning("state_update_failed", error=str(exc))

    async def run_async(self) -> None:
        """Async collection loop for use as a server background task.

        Runs ``_collect()`` on the configured interval using asyncio.sleep
        instead of the synchronous Scheduler. Cancel the task to stop.
        """
        import asyncio

        setup_logging(verbose=True)
        logger.info(
            "collector_async_starting",
            interval=self.config.agent.interval_seconds,
            lookback_hours=self.config.agent.query_lookback_hours,
        )

        try:
            from auralake_backend.db.engine import init_engine

            init_engine(self.config.database.url)
        except Exception as exc:
            logger.error("db_init_failed", error=str(exc))
            return

        while True:
            self._collect()
            await asyncio.sleep(self.config.agent.interval_seconds)

    def _setup_signal_handlers(self) -> None:
        """Handle graceful shutdown."""

        def handler(signum, frame):
            logger.info("signal_received", signal=signum)
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
