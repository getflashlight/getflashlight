from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb

from flashlight.focus import sql_mapping
from flashlight.focus.enums import ComputeClass, ServiceCategory
from flashlight.ingest.base import IngestWindow
from flashlight.ingest.connectors._focus_map import map_focus_row
from flashlight.ingest.connectors.databricks import (
    _compute_class_sql,
    _csv_source_sql,
    _warehouse_sort_key,
    compute_class_for_sku,
)


@dataclass
class _FakeWarehouse:
    id: str
    name: str | None = None
    cluster_size: str | None = None
    enable_serverless_compute: bool | None = None


def _pick(*warehouses: _FakeWarehouse) -> str:
    return min(warehouses, key=_warehouse_sort_key).id


def test_warehouse_autopick_prefers_smallest_size() -> None:
    chosen = _pick(
        _FakeWarehouse(id="lg", cluster_size="Large"),
        _FakeWarehouse(id="sm", cluster_size="Small"),
        _FakeWarehouse(id="md", cluster_size="Medium"),
        _FakeWarehouse(id="xs", cluster_size="2X-Small"),
    )
    assert chosen == "xs"


def test_warehouse_autopick_serverless_breaks_size_tie() -> None:
    # Same size → serverless wins (no idle infra cost).
    chosen = _pick(
        _FakeWarehouse(id="classic", cluster_size="Small", enable_serverless_compute=False),
        _FakeWarehouse(id="serverless", cluster_size="Small", enable_serverless_compute=True),
    )
    assert chosen == "serverless"


def test_warehouse_autopick_unknown_size_sorts_last() -> None:
    chosen = _pick(
        _FakeWarehouse(id="unknown", cluster_size=None),
        _FakeWarehouse(id="big", cluster_size="4X-Large"),
    )
    assert chosen == "big"


def test_warehouse_autopick_name_is_final_tiebreak() -> None:
    # Identical size + serverless → deterministic by name.
    chosen = _pick(
        _FakeWarehouse(id="b", name="b-wh", cluster_size="Small", enable_serverless_compute=True),
        _FakeWarehouse(id="a", name="a-wh", cluster_size="Small", enable_serverless_compute=True),
    )
    assert chosen == "a"


def test_compute_class_from_sku() -> None:
    assert compute_class_for_sku("ENTERPRISE_JOBS_SERVERLESS_COMPUTE") == ComputeClass.SERVERLESS
    assert compute_class_for_sku("STANDARD_ALL_PURPOSE_COMPUTE") == ComputeClass.CLASSIC
    assert compute_class_for_sku(None) == ComputeClass.CLASSIC


def test_maps_databricks_focus_1_3_output_row() -> None:
    # Shape mirrors the vendored query's output columns.
    row = {
        "ProviderName": "Databricks",
        "BillingAccountId": "acct-123",
        "SubAccountId": "ws-9",
        "SubAccountName": "analytics-ws",
        "BillingPeriodStart": "2026-06-01",
        "BillingPeriodEnd": "2026-07-01",
        "ChargePeriodStart": "2026-06-15 10:00:00",
        "ChargePeriodEnd": "2026-06-15 11:00:00",
        "BillingCurrency": "USD",
        "BilledCost": "12.5",
        "EffectiveCost": "12.5",
        "ListCost": "20.0",
        "ContractedCost": "12.5",
        "ChargeCategory": "Usage",
        "ServiceCategory": "Analytics",
        "ServiceName": "ALL_PURPOSE",
        "SkuId": "STANDARD_ALL_PURPOSE_COMPUTE",
        "ResourceId": "0617-cluster-abc",
        "ConsumedQuantity": "3.0",
        "ConsumedUnit": "DBU",
        "Tags": {"team": "data-eng"},
    }
    rec = map_focus_row(row, "databricks")
    assert rec is not None
    assert rec.provider_name == "Databricks"
    assert rec.service_category == ServiceCategory.ANALYTICS
    assert rec.resource_id == "0617-cluster-abc"  # cluster id → joins to AWS ClusterId tag
    assert str(rec.effective_cost) == "12.5"
    assert rec.tags == {"team": "data-eng"}
    assert compute_class_for_sku(rec.sku_id) == ComputeClass.CLASSIC


def _csv_row(**overrides: str) -> str:
    cols = list(sql_mapping.FOCUS_COLUMNS) + ["x_RecordId", "x_RecordType"]
    return ",".join(overrides.get(c, "") for c in cols)


def test_csv_source_sql_drops_leading_header_row_from_first_chunk(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Databricks' EXTERNAL_LINKS/CSV disposition isn't documented on whether a
    chunk file starts with its own header line — observed in practice to
    sometimes be true for the first chunk only, which would otherwise ingest a
    bogus row (every field equal to its own column name, e.g.
    BillingCurrency='BillingCurrency') and trip the single-currency assert.
    ``_csv_source_sql`` must detect and skip that header row on chunk 0, while a
    later chunk with no header of its own keeps all of its real data rows.
    """
    columns = list(sql_mapping.FOCUS_COLUMNS) + ["x_RecordId", "x_RecordType"]
    base = {
        "ProviderName": "Databricks",
        "BillingAccountId": "acct-1",
        "BillingCurrency": "USD",
        "ChargeCategory": "Usage",
        "ServiceCategory": "Analytics",
        "ResourceId": "cluster-1",
        "x_RecordId": "rec-A",
        "x_RecordType": "ORIGINAL",
    }
    chunk0 = tmp_path / "chunk0.csv"
    chunk0.write_text(",".join(columns) + "\n" + _csv_row(**base) + "\n")
    chunk1 = tmp_path / "chunk1.csv"
    chunk1.write_text(_csv_row(**{**base, "x_RecordId": "rec-B"}) + "\n")

    con = duckdb.connect()
    source_sql = _csv_source_sql(con, [str(chunk0), str(chunk1)], columns)
    rows = con.execute(
        f"SELECT BillingCurrency, x_RecordId FROM {source_sql} ORDER BY x_RecordId"  # noqa: S608
    ).fetchall()

    assert rows == [("USD", "rec-A"), ("USD", "rec-B")]


def test_vectorized_mapping_keeps_corrections_distinct_and_stamps_compute_class(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """The vectorized ingest() path (DuckDB reading CSV chunks directly, see
    ``DatabricksConnector.ingest``) must reproduce what the old row-based ``fetch()``
    did: x_compute_class from the SKU, x_effective_is_list from the connector, and —
    critically — x_record_id/x_record_type feeding the dedupe key so two DIFFERENT
    correction records for the same charge don't collide into one row.
    """
    base = {
        "ProviderName": "Databricks",
        "BillingAccountId": "acct-1",
        "SubAccountId": "ws-1",
        "BillingPeriodStart": "2026-06-01",
        "BillingPeriodEnd": "2026-06-30",
        "ChargePeriodStart": "2026-06-15 00:00:00",
        "ChargePeriodEnd": "2026-06-15 01:00:00",
        "BillingCurrency": "USD",
        "ChargeCategory": "Usage",
        "ChargeClass": "Correction",
        "ServiceCategory": "Analytics",
        "ServiceName": "ALL_PURPOSE",
        "SkuId": "STANDARD_ALL_PURPOSE_COMPUTE",
        "ResourceId": "cluster-1",
        "ConsumedQuantity": "1",
        "ConsumedUnit": "DBU",
        "Tags": "{}",
    }
    # Same charge line, two distinct correction records — must both survive.
    retraction = _csv_row(**base, BilledCost="-10", EffectiveCost="-10", ListCost="-10",
                           ContractedCost="-10", x_RecordId="rec-A", x_RecordType="RETRACTION")
    restatement = _csv_row(**base, BilledCost="-20", EffectiveCost="-20", ListCost="-20",
                            ContractedCost="-20", x_RecordId="rec-B", x_RecordType="RESTATEMENT")
    # A distinct charge, serverless SKU — proves the compute-class classifier.
    serverless = _csv_row(
        **{
            **base,
            "ChargePeriodStart": "2026-06-16 00:00:00",
            "ChargePeriodEnd": "2026-06-16 01:00:00",
            "ChargeClass": "",
            "SkuId": "ENTERPRISE_JOBS_SERVERLESS_COMPUTE",
            "ServiceName": "JOBS",
        },
        BilledCost="5", EffectiveCost="5", ListCost="5", ContractedCost="5",
        x_RecordId="rec-C", x_RecordType="ORIGINAL",
    )
    csv_path = tmp_path / "chunk.csv"
    csv_path.write_text("\n".join([retraction, restatement, serverless]) + "\n")

    columns = list(sql_mapping.FOCUS_COLUMNS) + ["x_RecordId", "x_RecordType"]
    con = duckdb.connect()
    sql_mapping.ensure_helpers(con)
    source_sql = _csv_source_sql(con, [str(csv_path)], columns)
    present = sql_mapping.present_columns(con, source_sql)
    mapped = sql_mapping.mapping_sql(
        source_sql,
        connector="databricks",
        run_id="r1",
        focus_version="1.3",
        present=present,
        compute_class_sql=_compute_class_sql(),
        effective_is_list=True,
    )
    rows = con.execute(
        f"SELECT x_record_id, x_record_type, x_compute_class, x_effective_is_list, "  # noqa: S608
        f"dedupe_key FROM ({mapped}) ORDER BY x_record_id"
    ).fetchall()

    assert len(rows) == 3  # retraction + restatement didn't collide
    by_id = {r[0]: r for r in rows}
    assert by_id["rec-A"][1] == "RETRACTION"
    assert by_id["rec-B"][1] == "RESTATEMENT"
    assert by_id["rec-A"][4] != by_id["rec-B"][4]  # distinct dedupe keys
    assert by_id["rec-A"][2] == ComputeClass.CLASSIC.value
    assert by_id["rec-C"][2] == ComputeClass.SERVERLESS.value
    assert all(r[3] for r in rows)  # x_effective_is_list stamped True everywhere


# ── AI serving usage (the token plane) ───────────────────────────────────────
# No WorkspaceClient is mocked anywhere here: the mapper is a pure staticmethod and the
# degradation ladder is exercised by stubbing only `_execute`, the single seam that talks to
# the warehouse.
def _ai_row(**kw: object) -> dict[str, object]:
    """A raw databricks_ai_usage.sql result row — every value a string, as the
    Statement Execution API delivers them."""
    base: dict[str, object] = {
        "endpoint_id": "ep-1",
        "endpoint_name": "chat",
        "served_entity_id": "se-1",
        "model_name": "llama-3-70b",
        "model_version": "2",
        "model_kind": "FOUNDATION_MODEL",
        "serving_mode": "pay_per_token",
        "requester": "alice@example.com",
        "usage_context_project": "rag",
        "scale_to_zero_enabled": "true",
        "workload_size": "Small",
        "workload_type": "GPU_SMALL",
        "min_provisioned_throughput": "",
        "max_provisioned_throughput": "",
        "charge_month": "2026-05-01",
        "request_count": "120",
        "error_request_count": "4",
        "input_tokens": "900000",
        "output_tokens": "100000",
        "error_input_tokens": "3000",
        "error_output_tokens": "500",
        "total_duration_ms": "45000",
    }
    return {**base, **kw}


def test_to_ai_usage_maps_a_row() -> None:
    from flashlight.ingest.connectors.databricks import DatabricksConnector

    record = DatabricksConnector._to_ai_usage(_ai_row())
    assert record is not None
    assert record.endpoint_id == "ep-1"
    assert record.model_name == "llama-3-70b"
    assert record.serving_mode == "pay_per_token"
    assert record.requester == "alice@example.com"
    assert record.usage_context_project == "rag"
    assert record.scale_to_zero_enabled is True
    assert record.workload_type == "GPU_SMALL"
    # Empty strings are absent, not 0.0 — an unset provisioned throughput is unmeasured.
    assert record.min_provisioned_throughput is None
    assert record.input_tokens == 900_000
    assert record.error_request_count == 4
    assert record.charge_month == date(2026, 5, 1)
    assert record.x_source_connector == "databricks"


def test_to_ai_usage_skips_rows_it_cannot_place_or_join() -> None:
    """No endpoint id → nothing to join cost to; no month → no partition. A synthetic key
    for either would invent a phantom endpoint."""
    from flashlight.ingest.connectors.databricks import DatabricksConnector

    assert DatabricksConnector._to_ai_usage(_ai_row(endpoint_id="")) is None
    assert DatabricksConnector._to_ai_usage(_ai_row(charge_month="")) is None


def test_to_ai_usage_normalizes_an_unrecognized_serving_mode() -> None:
    """A mode we don't understand must not reach GOLD claiming to be understood — GOLD
    withholds the $/token claim for 'unknown'."""
    from flashlight.ingest.connectors.databricks import DatabricksConnector

    record = DatabricksConnector._to_ai_usage(_ai_row(serving_mode="brand_new_mode"))
    assert record is not None and record.serving_mode == "unknown"


def test_opt_bool_distinguishes_measured_false_from_unmeasured() -> None:
    """scale_to_zero_enabled=False may fire a rule; NULL must not. Coercing an unknown
    value to False would invent a finding."""
    from flashlight.ingest.connectors.databricks import _opt_bool

    assert _opt_bool("true") is True
    assert _opt_bool("false") is False
    assert _opt_bool(True) is True
    assert _opt_bool("") is None
    assert _opt_bool(None) is None
    assert _opt_bool("maybe") is None


def _connector(monkeypatch: Any, execute: Any) -> Any:
    """A DatabricksConnector with the two seams that reach outward replaced.

    ``WorkspaceClient`` is stubbed because ``__init__`` constructs it eagerly and the real
    SDK blocks on auth discovery against the fake host. ``_execute`` is the single seam that
    talks to a warehouse, so replacing it is enough to drive the whole probe ladder — no
    mocked SDK surface, matching how test_aws_export_setup stubs `_bcm_client`.
    """
    from flashlight.ingest import config as config_mod
    from flashlight.ingest.connectors import databricks as db_mod

    monkeypatch.setattr(db_mod, "WorkspaceClient", lambda **_kw: object())
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    connector = db_mod.DatabricksConnector(
        config_mod.DatabricksConfig(
            type="databricks",
            host="https://example.cloud.databricks.com",
            token_env="DATABRICKS_TOKEN",
        )
    )
    monkeypatch.setattr(connector, "_execute", execute)
    return connector


def _connector_with_execute(
    monkeypatch: Any, responses: dict[str, list[dict[str, object]]]
) -> Any:
    """A connector whose warehouse seam returns canned rows, matched by SQL substring."""

    def _execute(sql: str) -> list[dict[str, object]]:
        for needle, rows in responses.items():
            if needle in sql:
                return rows
        return []

    return _connector(monkeypatch, _execute)


def test_efficiency_query_defaults_to_the_literal_project_tag_key(monkeypatch: Any) -> None:
    """Unset ``project_tag_key`` must render identically to the value this file shipped
    with before it became configurable — a silent behavior change here would be worse
    than the hardcoded literal it replaces."""
    connector = _connector_with_execute(monkeypatch, {"account_prices": []})
    sql = connector._render_efficiency_query(IngestWindow(date(2026, 1, 1), date(2026, 1, 31)))
    assert "element_at(u.custom_tags, 'project')" in sql
    assert ":project_tag_key" not in sql


def test_efficiency_query_substitutes_configured_project_tag_key(monkeypatch: Any) -> None:
    """An org whose project-equivalent custom tag isn't literally spelled 'project' can
    point the efficiency pull at its own key instead — see DatabricksConfig.project_tag_key."""
    from flashlight.ingest import config as config_mod
    from flashlight.ingest.connectors import databricks as db_mod

    monkeypatch.setattr(db_mod, "WorkspaceClient", lambda **_kw: object())
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    connector = db_mod.DatabricksConnector(
        config_mod.DatabricksConfig(
            type="databricks",
            host="https://example.cloud.databricks.com",
            token_env="DATABRICKS_TOKEN",
            project_tag_key="cost_center",
        )
    )
    monkeypatch.setattr(connector, "_execute", lambda _sql: [])
    sql = connector._render_efficiency_query(IngestWindow(date(2026, 1, 1), date(2026, 1, 31)))
    assert "element_at(u.custom_tags, 'cost_center')" in sql
    assert "element_at(u.custom_tags, 'project')" not in sql


def test_serving_probe_full_when_both_tables_exist(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    connector = _connector_with_execute(
        monkeypatch,
        {"'endpoint_usage'": [{"1": "1"}], "'served_entities'": [{"1": "1"}]},
    )
    assert connector._resolve_serving_tables() == "full"


def test_serving_probe_degrades_to_usage_only_without_served_entities(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Rung 2: tokens and requester still land. Model identity doesn't, so serving_mode
    stays 'unknown' and GOLD withholds every $/token figure rather than guessing one."""
    connector = _connector_with_execute(monkeypatch, {"'endpoint_usage'": [{"1": "1"}]})
    assert connector._resolve_serving_tables() == "usage_only"

    sql = connector._render_ai_usage_query(
        IngestWindow(date(2026, 5, 1), date(2026, 5, 31)), entities=False
    )
    # The CTE is stubbed out rather than the projection being duplicated, so the LEFT JOIN
    # matches nothing and every served_entities column comes back NULL.
    assert "system.serving.served_entities" not in sql
    assert "WHERE 1 = 0" in sql
    assert "system.serving.endpoint_usage" in sql
    # The projection is untouched — same column list either way.
    assert "AS serving_mode" in sql
    assert ":start_date" not in sql and ":end_date" not in sql


def test_serving_probe_none_skips_the_pull_entirely(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Rung 3, and the invariant that keeps `idle` honest: an unmeasured endpoint produces
    NO row, so a measured zero can never be confused with silence."""
    connector = _connector_with_execute(monkeypatch, {})
    assert connector._resolve_serving_tables() == "none"
    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    assert list(connector.fetch_ai_usage(window)) == []


def test_serving_probe_failure_is_not_fatal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The probe shares the auth path with the cost query, so a broken connection must let
    the cost pull report the real error rather than dying here."""
    from flashlight.core.exceptions import ConnectorError

    def _boom(_sql: str) -> list[dict[str, object]]:
        raise ConnectorError("databricks", "expired token")

    connector = _connector(monkeypatch, _boom)
    assert connector._resolve_serving_tables() == "none"


def test_ai_usage_query_binds_the_window(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    connector = _connector_with_execute(
        monkeypatch,
        {"'endpoint_usage'": [{"1": "1"}], "'served_entities'": [{"1": "1"}]},
    )
    sql = connector._render_ai_usage_query(
        IngestWindow(date(2026, 5, 1), date(2026, 5, 31)), entities=True
    )
    assert "'2026-05-01'" in sql and "'2026-05-31'" in sql
    assert ":start_date" not in sql and ":end_date" not in sql
    assert "system.serving.served_entities" in sql
