from decimal import Decimal

from flashlight.focus.enums import (
    ChargeCategory,
    CommitmentDiscountCategory,
    CommitmentDiscountStatus,
    ServiceCategory,
)
from flashlight.ingest.connectors._focus_map import map_focus_row


def _row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ProviderName": "Microsoft",
        "BillingAccountId": "acct-1",
        "BillingPeriodStart": "2024-09-01",
        "BillingPeriodEnd": "2024-09-30",
        "ChargePeriodStart": "2024-09-18 22:00:00",
        "ChargePeriodEnd": "2024-09-18 23:00:00",
        "BillingCurrency": "USD",
        "EffectiveCost": "1.5",
        "BilledCost": "1.5",
        "ChargeCategory": "Usage",
        "ChargeClass": "NULL",
        "ServiceCategory": "Compute",
        "ServiceName": "Virtual Machines",
        "ResourceId": "NULL",
        "Tags": "NULL",
    }
    base.update(over)
    return base


def test_open_provider_name_preserved() -> None:
    # Microsoft/Oracle aren't in our enum constants — must pass through as strings.
    microsoft = map_focus_row(_row(), "test_connector")
    oracle = map_focus_row(_row(ProviderName="Oracle"), "test_connector")
    assert microsoft is not None and microsoft.provider_name == "Microsoft"
    assert oracle is not None and oracle.provider_name == "Oracle"


def test_null_tokens_become_none() -> None:
    rec = map_focus_row(_row(), "test_connector")
    assert rec is not None
    assert rec.resource_id is None  # "NULL" -> None
    assert rec.charge_class is None  # "NULL" -> None
    assert rec.tags == {}  # "NULL" -> {}


def test_space_separated_timestamp_parsed() -> None:
    rec = map_focus_row(_row(), "test_connector")
    assert rec is not None and rec.charge_period_start.hour == 22


def test_credit_and_unknown_category_fallbacks() -> None:
    credit = map_focus_row(_row(ChargeCategory="Credit"), "f")
    assert credit is not None and credit.charge_category == ChargeCategory.CREDIT
    # FOCUS 1.0 'Identity' isn't in our enum → OTHER, but cost is unaffected.
    rec = map_focus_row(_row(ServiceCategory="Identity"), "f")
    assert rec is not None and rec.service_category == ServiceCategory.OTHER


def test_tags_from_duckdb_map_pairs() -> None:
    # DuckDB returns a Parquet MAP (AWS FOCUS Tags) as a list of (k, v) pairs.
    rec = map_focus_row(_row(Tags=[("aws:eks:cluster-name", "prod-eks"), ("env", "prod")]), "f")
    assert rec is not None
    assert rec.tags == {"aws:eks:cluster-name": "prod-eks", "env": "prod"}


def test_missing_charge_period_skipped() -> None:
    assert map_focus_row(_row(ChargePeriodStart="NULL"), "f") is None


def test_costs_parsed_as_decimal() -> None:
    rec = map_focus_row(_row(EffectiveCost="-3.0"), "f")
    assert rec is not None and rec.effective_cost == Decimal("-3.0")


def test_commitment_and_invoice_fields_mapped() -> None:
    rec = map_focus_row(
        _row(
            CommitmentDiscountId="cud-1",
            CommitmentDiscountType="SavingsPlan",
            CommitmentDiscountCategory="Spend",
            CommitmentDiscountStatus="Unused",
            CommitmentDiscountQuantity="2.5",
            CommitmentDiscountUnit="Hrs",
            InvoiceId="inv-1",
            InvoiceIssuerName="Amazon Web Services",
        ),
        "f",
    )
    assert rec is not None
    assert rec.commitment_discount_id == "cud-1"
    assert rec.commitment_discount_category == CommitmentDiscountCategory.SPEND
    assert rec.commitment_discount_status == CommitmentDiscountStatus.UNUSED
    assert rec.commitment_discount_quantity == 2.5
    assert rec.invoice_id == "inv-1"
    assert rec.invoice_issuer_name == "Amazon Web Services"


def test_commitment_fields_absent_default_none() -> None:
    rec = map_focus_row(_row(), "f")
    assert rec is not None
    assert rec.commitment_discount_id is None
    assert rec.commitment_discount_category is None
    assert rec.invoice_id is None


def test_unknown_commitment_category_becomes_none() -> None:
    rec = map_focus_row(_row(CommitmentDiscountCategory="Bogus"), "f")
    assert rec is not None and rec.commitment_discount_category is None
