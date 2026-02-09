"""Abstract interfaces for lakehouse providers.

Every platform (Databricks, Snowflake, Lake Formation) implements these
interfaces so that analyzers, actions, and CLI commands remain platform-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Any

from auralake.models.billing import (
    CostBreakdown,
    CostRecord,
    DataTransferCost,
    InfraComputeCost,
    InfraStorageCost,
    PriceRecord,
    RISavingsPlanRec,
    ResourceMapping,
)
from auralake.models.compute import ClusterInfo, ClusterUtilization
from auralake.models.config import AuraLakeConfig
from auralake.models.jobs import JobProfile


# ---- Cost Clients ----

class AbstractCostClient(ABC):
    """Platform-level cost data (e.g., Databricks DBU billing)."""

    @abstractmethod
    def get_usage(self, start: date, end: date, group_by: list[str] | None = None) -> list[CostRecord]:
        ...

    @abstractmethod
    def get_pricing(self) -> list[PriceRecord]:
        ...

    @abstractmethod
    def get_cost_breakdown(self, start: date, end: date) -> CostBreakdown:
        ...


class AbstractInfraCostClient(ABC):
    """Underlying infrastructure cost (e.g., AWS EC2/S3/EBS for Databricks)."""

    @abstractmethod
    def get_compute_costs(self, start: date, end: date) -> list[InfraComputeCost]:
        ...

    @abstractmethod
    def get_storage_costs(self, start: date, end: date) -> list[InfraStorageCost]:
        ...

    @abstractmethod
    def get_data_transfer_costs(self, start: date, end: date) -> list[DataTransferCost]:
        ...

    @abstractmethod
    def map_platform_resources_to_infra(self) -> list[ResourceMapping]:
        ...

    @abstractmethod
    def get_ri_savings_plan_recommendations(self) -> list[RISavingsPlanRec]:
        ...


# ---- Compute Client ----

class AbstractComputeClient(ABC):
    """Unified compute resource management."""

    @abstractmethod
    def list_clusters(self) -> list[ClusterInfo]:
        ...

    @abstractmethod
    def get_cluster(self, cluster_id: str) -> ClusterInfo:
        ...

    @abstractmethod
    def get_utilization(self, cluster_id: str, days: int = 30) -> ClusterUtilization:
        ...

    @abstractmethod
    def resize(self, cluster_id: str, config: dict[str, Any]) -> None:
        ...

    @abstractmethod
    def terminate(self, cluster_id: str) -> None:
        ...


# ---- Storage Client ----

class AbstractStorageClient(ABC):
    """Unified storage/table operations."""

    @abstractmethod
    def list_tables(self, catalog: str | None = None, schema: str | None = None) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_table_stats(self, table_name: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def optimize_table(self, table_name: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def vacuum_table(self, table_name: str, retention_hours: int = 168) -> dict[str, Any]:
        ...


# ---- Job Client ----

class AbstractJobClient(ABC):
    """Unified job/workflow management."""

    @abstractmethod
    def list_jobs(self) -> list[JobProfile]:
        ...

    @abstractmethod
    def get_job(self, job_id: str) -> JobProfile:
        ...

    @abstractmethod
    def get_job_runs(self, job_id: str, limit: int = 25) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def cancel_run(self, run_id: str) -> None:
        ...


# ---- Query Client ----

class AbstractQueryClient(ABC):
    """Unified query history and execution."""

    @abstractmethod
    def get_query_history(self, hours: int = 24, limit: int = 1000) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def execute_query(self, sql: str) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def explain_query(self, sql: str) -> str:
        ...


# ---- Config Format ----

class AbstractConfigFormat(ABC):
    """Platform-specific config file format (DABs for Databricks, etc.)."""

    @abstractmethod
    def parse(self, path: Path) -> dict[str, Any]:
        ...

    @abstractmethod
    def modify_job(self, path: Path, job_name: str, changes: dict[str, Any]) -> str:
        ...

    @abstractmethod
    def modify_cluster(self, path: Path, cluster_name: str, changes: dict[str, Any]) -> str:
        ...


# ---- Provider ----

class AbstractProvider(ABC):
    """Each lakehouse platform implements this interface."""

    name: str

    def __init__(self, config: AuraLakeConfig) -> None:
        self.config = config

    @abstractmethod
    def get_cost_client(self) -> AbstractCostClient:
        ...

    @abstractmethod
    def get_infra_cost_client(self) -> AbstractInfraCostClient:
        ...

    @abstractmethod
    def get_compute_client(self) -> AbstractComputeClient:
        ...

    @abstractmethod
    def get_storage_client(self) -> AbstractStorageClient:
        ...

    @abstractmethod
    def get_job_client(self) -> AbstractJobClient:
        ...

    @abstractmethod
    def get_query_client(self) -> AbstractQueryClient:
        ...

    @abstractmethod
    def get_config_format(self) -> AbstractConfigFormat:
        ...
