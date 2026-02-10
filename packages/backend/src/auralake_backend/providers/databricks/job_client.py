"""Databricks Jobs API client."""

from __future__ import annotations

from typing import Any

from auralake_shared.core.exceptions import APIError
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.models.jobs import JobProfile
from auralake_shared.providers.base import AbstractJobClient

from auralake_backend.providers.databricks.auth import get_workspace_client


class DatabricksJobClient(AbstractJobClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def list_jobs(self) -> list[JobProfile]:
        try:
            jobs = self._client.jobs.list()
            return [self._to_job_profile(j) for j in jobs]
        except Exception as exc:
            raise APIError("databricks", f"Failed to list jobs: {exc}") from exc

    def get_job(self, job_id: str) -> JobProfile:
        try:
            j = self._client.jobs.get(int(job_id))
            return self._to_job_profile(j)
        except Exception as exc:
            raise APIError("databricks", f"Failed to get job {job_id}: {exc}") from exc

    def get_job_runs(self, job_id: str, limit: int = 25) -> list[dict[str, Any]]:
        try:
            runs = self._client.jobs.list_runs(job_id=int(job_id), limit=limit)
            result = []
            for run in runs:
                result.append(
                    {
                        "run_id": str(run.run_id),
                        "state": str(run.state.result_state.value)
                        if run.state and run.state.result_state
                        else "UNKNOWN",
                        "start_time": str(run.start_time) if run.start_time else None,
                        "end_time": str(run.end_time) if run.end_time else None,
                        "execution_duration_ms": run.execution_duration,
                    }
                )
            return result
        except Exception as exc:
            raise APIError("databricks", f"Failed to list runs for job {job_id}: {exc}") from exc

    def cancel_run(self, run_id: str) -> None:
        try:
            self._client.jobs.cancel_run(int(run_id))
        except Exception as exc:
            raise APIError("databricks", f"Failed to cancel run {run_id}: {exc}") from exc

    @staticmethod
    def _to_job_profile(j) -> JobProfile:
        settings = j.settings if j.settings else None
        schedule = None
        if settings and settings.schedule:
            schedule = settings.schedule.quartz_cron_expression

        # Extract cluster info from first task if available
        instance_type = None
        worker_count = 0
        if settings and settings.tasks:
            for task in settings.tasks:
                if task.new_cluster:
                    nc = task.new_cluster
                    instance_type = nc.node_type_id
                    worker_count = nc.num_workers or 0
                    break

        return JobProfile(
            job_id=str(j.job_id),
            job_name=settings.name if settings else str(j.job_id),
            schedule_cron=schedule,
            instance_type=instance_type,
            worker_count=worker_count,
        )
