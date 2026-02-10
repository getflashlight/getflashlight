"""Lake Formation provider — stub implementation for future development."""

from __future__ import annotations

from auralake_shared.core.exceptions import ProviderError
from auralake_shared.models.config import AuraLakeConfig
from auralake_shared.providers import register_provider
from auralake_shared.providers.base import (
    AbstractComputeClient,
    AbstractConfigFormat,
    AbstractCostClient,
    AbstractInfraCostClient,
    AbstractJobClient,
    AbstractProvider,
    AbstractQueryClient,
    AbstractStorageClient,
)


class LakeFormationProvider(AbstractProvider):
    name = "lake_formation"

    def __init__(self, config: AuraLakeConfig) -> None:
        super().__init__(config)

    def get_cost_client(self) -> AbstractCostClient:
        raise ProviderError("lake_formation", "Lake Formation cost client not yet implemented")

    def get_infra_cost_client(self) -> AbstractInfraCostClient:
        raise ProviderError(
            "lake_formation", "Lake Formation infra cost client not yet implemented"
        )

    def get_compute_client(self) -> AbstractComputeClient:
        raise ProviderError("lake_formation", "Lake Formation compute client not yet implemented")

    def get_storage_client(self) -> AbstractStorageClient:
        raise ProviderError("lake_formation", "Lake Formation storage client not yet implemented")

    def get_job_client(self) -> AbstractJobClient:
        raise ProviderError("lake_formation", "Lake Formation job client not yet implemented")

    def get_query_client(self) -> AbstractQueryClient:
        raise ProviderError("lake_formation", "Lake Formation query client not yet implemented")

    def get_config_format(self) -> AbstractConfigFormat:
        raise ProviderError("lake_formation", "Lake Formation config format not yet implemented")


register_provider("lake_formation", LakeFormationProvider)
