"""Databricks provider implementation.

Wires up all Databricks and AWS client factories and registers itself
with the auralake provider registry.
"""

from __future__ import annotations

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


class DatabricksProvider(AbstractProvider):
    name = "databricks"

    def __init__(self, config: AuraLakeConfig) -> None:
        super().__init__(config)
        self._db_config = config.databricks
        self._aws_config = config.databricks.aws

    def get_cost_client(self) -> AbstractCostClient:
        from auralake_backend.providers.databricks.cost_client import DatabricksCostClient

        return DatabricksCostClient(self._db_config)

    def get_infra_cost_client(self) -> AbstractInfraCostClient:
        from auralake_backend.providers.databricks.aws.cost_explorer import (
            AWSCostExplorerClient,
        )

        return AWSCostExplorerClient(self._db_config, self._aws_config)

    def get_compute_client(self) -> AbstractComputeClient:
        from auralake_backend.providers.databricks.compute_client import (
            DatabricksComputeClient,
        )

        return DatabricksComputeClient(self._db_config)

    def get_storage_client(self) -> AbstractStorageClient:
        from auralake_backend.providers.databricks.storage_client import (
            DatabricksStorageClient,
        )

        return DatabricksStorageClient(self._db_config)

    def get_job_client(self) -> AbstractJobClient:
        from auralake_backend.providers.databricks.job_client import (
            DatabricksJobClient,
        )

        return DatabricksJobClient(self._db_config)

    def get_query_client(self) -> AbstractQueryClient:
        from auralake_backend.providers.databricks.query_client import (
            DatabricksQueryClient,
        )

        return DatabricksQueryClient(self._db_config)

    def get_config_format(self) -> AbstractConfigFormat:
        from auralake_backend.providers.databricks.dab_parser import DABConfigFormat

        return DABConfigFormat(self.config)


register_provider("databricks", DatabricksProvider)
