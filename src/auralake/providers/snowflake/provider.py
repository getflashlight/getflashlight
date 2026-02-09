"""Snowflake provider — stub implementation for future development."""
from __future__ import annotations

from auralake.core.exceptions import ProviderError
from auralake.models.config import AuraLakeConfig
from auralake.providers import register_provider
from auralake.providers.base import (
    AbstractComputeClient,
    AbstractConfigFormat,
    AbstractCostClient,
    AbstractInfraCostClient,
    AbstractJobClient,
    AbstractProvider,
    AbstractQueryClient,
    AbstractStorageClient,
)


class SnowflakeProvider(AbstractProvider):
    name = "snowflake"

    def __init__(self, config: AuraLakeConfig) -> None:
        super().__init__(config)

    def get_cost_client(self) -> AbstractCostClient:
        raise ProviderError("snowflake", "Snowflake cost client not yet implemented")

    def get_infra_cost_client(self) -> AbstractInfraCostClient:
        raise ProviderError("snowflake", "Snowflake infra cost client not yet implemented")

    def get_compute_client(self) -> AbstractComputeClient:
        raise ProviderError("snowflake", "Snowflake compute client not yet implemented")

    def get_storage_client(self) -> AbstractStorageClient:
        raise ProviderError("snowflake", "Snowflake storage client not yet implemented")

    def get_job_client(self) -> AbstractJobClient:
        raise ProviderError("snowflake", "Snowflake job client not yet implemented")

    def get_query_client(self) -> AbstractQueryClient:
        raise ProviderError("snowflake", "Snowflake query client not yet implemented")

    def get_config_format(self) -> AbstractConfigFormat:
        raise ProviderError("snowflake", "Snowflake config format not yet implemented")


register_provider("snowflake", SnowflakeProvider)
