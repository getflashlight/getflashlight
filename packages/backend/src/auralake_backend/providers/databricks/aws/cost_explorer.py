"""AWS Cost Explorer client — infrastructure cost layer for Databricks."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from auralake_shared.core.exceptions import APIError
from auralake_shared.models.billing import (
    DataTransferCost,
    InfraComputeCost,
    InfraStorageCost,
    ResourceMapping,
    RISavingsPlanRec,
)
from auralake_shared.models.config import DatabricksAWSConfig, DatabricksConfig
from auralake_shared.providers.base import AbstractInfraCostClient

from auralake_backend.providers.databricks.auth import get_boto3_session
from auralake_backend.providers.databricks.pricing import PricingService


class AWSCostExplorerClient(AbstractInfraCostClient):
    def __init__(self, db_config: DatabricksConfig, aws_config: DatabricksAWSConfig) -> None:
        self._db_config = db_config
        self._aws_config = aws_config
        self._pricing_service = PricingService(db_config.discounts)
        session = get_boto3_session(aws_config.region, aws_config=aws_config)
        self._ce = session.client("ce")
        self._ec2 = session.client("ec2")

    def _paginate_cost_and_usage(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Paginate through get_cost_and_usage results."""
        all_results: list[dict[str, Any]] = []
        while True:
            response = self._ce.get_cost_and_usage(**kwargs)
            all_results.extend(response.get("ResultsByTime", []))
            token = response.get("NextPageToken")
            if not token:
                break
            kwargs["NextPageToken"] = token
        return all_results

    def get_compute_costs(self, start: date, end: date) -> list[InfraComputeCost]:
        try:
            tag_filters = self._aws_config.cost_explorer.tag_filters
            filter_expr = self._build_tag_filter(tag_filters)
            and_conditions = [
                {
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": ["Amazon Elastic Compute Cloud - Compute"],
                    }
                },
            ]
            if filter_expr:
                and_conditions.append(filter_expr)

            time_periods = self._paginate_cost_and_usage(
                TimePeriod={"Start": str(start), "End": str(end)},
                Granularity="DAILY",
                Metrics=["UnblendedCost", "UsageQuantity"],
                Filter={"And": and_conditions},
                GroupBy=[
                    {"Type": "DIMENSION", "Key": "RESOURCE_ID"},
                ],
            )
            results = []
            for time_period in time_periods:
                p_start = date.fromisoformat(time_period["TimePeriod"]["Start"])
                p_end = date.fromisoformat(time_period["TimePeriod"]["End"])
                for group in time_period.get("Groups", []):
                    resource_id = group["Keys"][0]
                    cost = Decimal(group["Metrics"]["UnblendedCost"]["Amount"])
                    cost = self._pricing_service.apply_aws_discount(cost)
                    hours = float(group["Metrics"]["UsageQuantity"]["Amount"])
                    results.append(
                        InfraComputeCost(
                            resource_id=resource_id,
                            service="AmazonEC2",
                            cost_usd=cost,
                            usage_hours=hours,
                            period_start=p_start,
                            period_end=p_end,
                        )
                    )
            return results
        except Exception as exc:
            raise APIError(
                "databricks",
                f"AWS Cost Explorer compute query failed: {exc}",
            ) from exc

    def get_storage_costs(self, start: date, end: date) -> list[InfraStorageCost]:
        try:
            time_periods = self._paginate_cost_and_usage(
                TimePeriod={"Start": str(start), "End": str(end)},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost", "UsageQuantity"],
                Filter={
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": [
                            "Amazon Simple Storage Service",
                            "Amazon Elastic Block Store",
                        ],
                    }
                },
                GroupBy=[
                    {"Type": "DIMENSION", "Key": "SERVICE"},
                    {"Type": "DIMENSION", "Key": "RESOURCE_ID"},
                ],
            )
            results = []
            for time_period in time_periods:
                p_start = date.fromisoformat(time_period["TimePeriod"]["Start"])
                p_end = date.fromisoformat(time_period["TimePeriod"]["End"])
                for group in time_period.get("Groups", []):
                    service = group["Keys"][0]
                    resource_id = group["Keys"][1]
                    cost = Decimal(group["Metrics"]["UnblendedCost"]["Amount"])
                    cost = self._pricing_service.apply_aws_discount(cost)
                    results.append(
                        InfraStorageCost(
                            bucket_or_volume=resource_id,
                            service=service,
                            cost_usd=cost,
                            period_start=p_start,
                            period_end=p_end,
                        )
                    )
            return results
        except Exception as exc:
            raise APIError(
                "databricks",
                f"AWS storage cost query failed: {exc}",
            ) from exc

    def get_data_transfer_costs(self, start: date, end: date) -> list[DataTransferCost]:
        try:
            time_periods = self._paginate_cost_and_usage(
                TimePeriod={"Start": str(start), "End": str(end)},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost", "UsageQuantity"],
                Filter={
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": ["AWS Data Transfer"],
                    }
                },
                GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            )
            results = []
            for time_period in time_periods:
                p_start = date.fromisoformat(time_period["TimePeriod"]["Start"])
                p_end = date.fromisoformat(time_period["TimePeriod"]["End"])
                for group in time_period.get("Groups", []):
                    usage_type = group["Keys"][0]
                    cost = Decimal(group["Metrics"]["UnblendedCost"]["Amount"])
                    cost = self._pricing_service.apply_aws_discount(cost)
                    gb = float(group["Metrics"]["UsageQuantity"]["Amount"])
                    results.append(
                        DataTransferCost(
                            source=usage_type,
                            destination="",
                            cost_usd=cost,
                            transfer_gb=gb,
                            period_start=p_start,
                            period_end=p_end,
                        )
                    )
            return results
        except Exception as exc:
            raise APIError(
                "databricks",
                f"AWS data transfer cost query failed: {exc}",
            ) from exc

    def map_platform_resources_to_infra(self) -> list[ResourceMapping]:
        from auralake_backend.providers.databricks.aws.cluster_mapper import (
            ClusterMapper,
        )

        mapper = ClusterMapper(self._ec2, self._aws_config)
        return mapper.map_clusters()

    def get_ri_savings_plan_recommendations(self) -> list[RISavingsPlanRec]:
        try:
            response = self._ce.get_savings_plans_purchase_recommendation(
                SavingsPlansType="COMPUTE_SP",
                TermInYears="ONE_YEAR",
                PaymentOption="NO_UPFRONT",
                LookbackPeriodInDays="THIRTY_DAYS",
            )
            results = []
            rec_data = response.get("SavingsPlansPurchaseRecommendation", {})
            details = rec_data.get("SavingsPlansPurchaseRecommendationDetails", [])
            for detail in details:
                results.append(
                    RISavingsPlanRec(
                        resource_id=detail.get("InstanceFamily", "compute"),
                        current_cost_usd=Decimal(
                            detail.get("CurrentAverageHourlyOnDemandSpend", "0")
                        ),
                        recommended_commitment="1yr_savings_plan",
                        estimated_savings_usd=Decimal(
                            detail.get("EstimatedMonthlySavingsAmount", "0")
                        ),
                        breakeven_months=int(float(detail.get("PaybackPeriodInMonths", "0"))),
                    )
                )
            return results
        except Exception as exc:
            raise APIError(
                "databricks",
                f"AWS RI/SP recommendation query failed: {exc}",
            ) from exc

    @staticmethod
    def _build_tag_filter(tag_filters: dict[str, str]) -> dict:
        if not tag_filters:
            return {}
        conditions = []
        for key, value in tag_filters.items():
            conditions.append({"Tags": {"Key": key, "Values": [value]}})
        if len(conditions) == 1:
            return conditions[0]
        return {"And": conditions}
