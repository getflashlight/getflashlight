"""The dashboard's page ``Scope`` — the one thing that lets `/aws` reuse the shared
provider panels instead of forking them.

The load-bearing property is the identity in
:func:`test_unnarrowed_scope_builds_exactly_the_predicate_it_was_given`: for a page that
isn't narrowed, ``Scope.where`` produces byte-identical SQL to the hand-built
``WHERE`` clauses the panels used before. That's what makes converting every panel from
``group: str`` to ``Scope`` provably a no-op for the other providers.
"""

from __future__ import annotations

import pytest

from flashlight.dashboard.views.provider_focus import Scope
from flashlight.transform.catalog import PROVIDER_BASE_VIEWS, provider_view_dimensions

_REDSHIFT_ACCOUNT_WIDE = frozenset(
    {
        "credits_month",
        "commitment_summary_month",
        "spend_by_tag_key_month",
        "spend_by_tag_month",
        "spend_by_sku_tag_month",
        "spend_tag_coverage_month",
    }
)


def _redshift_scope() -> Scope:
    return Scope(
        group="aws",
        dimension="service_name",
        values=("Amazon Redshift", "Amazon Redshift Serverless"),
        account_wide=_REDSHIFT_ACCOUNT_WIDE,
    )


@pytest.mark.parametrize("spec", PROVIDER_BASE_VIEWS, ids=lambda s: s.view)
def test_unnarrowed_scope_builds_exactly_the_predicate_it_was_given(spec) -> None:  # type: ignore[no-untyped-def]
    """An un-narrowed page adds nothing — for EVERY provider base view.

    This is the whole safety argument for threading Scope through the shared panels:
    `/databricks`, `/microsoft`, `/oracle` and every future provider group must emit the
    same SQL they did before. Parameterized over the catalog rather than a sample so a
    newly added view can't quietly fall outside the guarantee.
    """
    scope = Scope(group="databricks")

    assert scope.narrowed is False
    assert scope.available(spec.view) is True
    assert scope.predicate(spec.view) == ""
    assert scope.where(spec.view) == ""
    assert scope.where(spec.view, "charge_month >= '2026-01-01'") == (
        "WHERE charge_month >= '2026-01-01'"
    )
    assert scope.where(spec.view, "a = 1", "b = 2") == "WHERE a = 1 AND b = 2"


def test_narrowed_scope_filters_a_view_that_carries_the_dimension() -> None:
    scope = _redshift_scope()

    assert scope.narrowed is True
    assert scope.available("spend_by_service_month") is True
    assert scope.predicate("spend_by_service_month") == (
        "service_name IN ('Amazon Redshift', 'Amazon Redshift Serverless')"
    )
    assert scope.where("spend_by_service_month", "charge_month >= '2026-01-01'") == (
        "WHERE charge_month >= '2026-01-01' "
        "AND service_name IN ('Amazon Redshift', 'Amazon Redshift Serverless')"
    )


def test_spend_trend_daily_is_scopable_because_the_view_was_widened() -> None:
    """The daily trend is the panel `/aws` had no way to render before.

    `spend_trend_daily` gained `service_name` precisely so a service-scoped page could
    have a daily series; if that dimension is ever dropped from the catalog, this fails
    rather than the chart silently widening to the whole AWS bill.
    """
    assert "service_name" in provider_view_dimensions("spend_trend_daily")
    assert _redshift_scope().available("spend_trend_daily") is True


def test_credits_are_account_wide_despite_carrying_service_name() -> None:
    """The precedence rule, and the reason `account_wide` is checked first.

    `credits_month` really does carry `service_name`, so a naive "the column exists, so
    filter on it" rule would scope it — hiding every account-level credit AWS didn't tag
    to a service, and understating the discount on a page whose own caption says credits
    are already netted into it.
    """
    assert "service_name" in provider_view_dimensions("credits_month")

    scope = _redshift_scope()
    assert scope.available("credits_month") is True
    assert scope.predicate("credits_month") == ""
    assert scope.where("credits_month", "charge_month >= '2026-01-01'") == (
        "WHERE charge_month >= '2026-01-01'"
    )


@pytest.mark.parametrize(
    "view",
    ["monthly_bill", "savings_summary_month", "sku_month_over_month", "spend_forecast_month"],
)
def test_group_wide_views_are_unavailable_when_narrowed(view: str) -> None:
    """A total, a percentage, a per-SKU variance and a forecast carry no service
    dimension, so under a narrowed scope they'd report the whole account's number under
    a Redshift heading. Panels must check `available()` and state the absence."""
    scope = _redshift_scope()

    assert scope.carries(view) is False
    assert scope.available(view) is False
    with pytest.raises(ValueError, match="carries no service_name"):
        scope.predicate(view)


def test_narrowed_scope_escapes_quotes_in_its_values() -> None:
    scope = Scope(group="aws", dimension="service_name", values=("O'Reilly Compute",))
    assert scope.predicate("spend_by_service_month") == (
        "service_name IN ('O''Reilly Compute')"
    )


def test_cost_subcategory_view_is_scopable_and_not_account_wide() -> None:
    """``spend_by_cost_subcategory_month`` must be narrowed on ``/aws``, not read whole.

    It carries ``service_name``, and the ``aws`` group now holds S3 as well as Redshift
    (``include_services`` defaults to both, so the Databricks backing-storage view has
    S3 cost to label). An unscoped read therefore grows a second pie titled "Amazon
    Simple Storage Service" under a Redshift heading — which is exactly the panel-reads-
    the-whole-group-by-design mistake ``Scope`` exists to make impossible.
    """
    scope = _redshift_scope()
    view = "spend_by_cost_subcategory_month"

    assert "service_name" in provider_view_dimensions(view)
    assert view not in _REDSHIFT_ACCOUNT_WIDE  # it is NOT an account-level figure
    assert scope.carries(view)
    assert scope.available(view)
    assert scope.predicate(view) != ""  # a real predicate reaches the SQL
    assert "service_name IN (" in scope.predicate(view)
