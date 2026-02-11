"""Discount-aware pricing service for Databricks DBU costs."""

from __future__ import annotations

from decimal import Decimal

from auralake_shared.models.config import DiscountConfig


class PricingService:
    """Wraps Databricks list prices with company-negotiated discounts."""

    def __init__(self, discounts: DiscountConfig) -> None:
        self._discounts = discounts

    def get_dbu_price(self, sku: str, list_price: Decimal) -> Decimal:
        """Return the effective $/DBU for a SKU after discounts.

        Priority: SKU override > global discount > list price.
        """
        db = self._discounts.databricks

        # SKU-specific negotiated price takes precedence
        if sku in db.sku_overrides:
            return Decimal(str(db.sku_overrides[sku]))

        # Global percentage discount
        if db.global_dbu_discount_pct > 0:
            factor = Decimal(str(1 - db.global_dbu_discount_pct))
            return list_price * factor

        return list_price

    def apply_aws_discount(self, cost: Decimal) -> Decimal:
        """Apply EDP discount to an AWS cost amount."""
        pct = self._discounts.aws.edp_discount_pct
        if pct > 0:
            return cost * Decimal(str(1 - pct))
        return cost

    def get_effective_prices(self, list_prices: dict[str, Decimal]) -> dict[str, Decimal]:
        """Return {sku: effective_price} for all SKUs."""
        return {sku: self.get_dbu_price(sku, price) for sku, price in list_prices.items()}
