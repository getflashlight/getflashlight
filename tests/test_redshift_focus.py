from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from flashlight.core.exceptions import ConnectorError
from flashlight.ingest.config import RedshiftConfig
from flashlight.ingest.connectors.aws_focus import _classify_redshift_cost_category
from flashlight.ingest.connectors.redshift import (
    _EARLIEST_RETAINED_SQL,
    RedshiftConnector,
    _field_value,
)

_BASTION_FIELDS = {
    "bastion_host": "bastion.example.com",
    "bastion_user": "ec2-user",
    "bastion_private_key_path": "/tmp/bastion-key.pem",
}


def test_redshift_config_requires_provisioned_cluster() -> None:
    with pytest.raises(ValidationError):
        RedshiftConfig.model_validate({"region": "us-east-1"})  # neither set


def test_redshift_config_rejects_legacy_workgroup() -> None:
    with pytest.raises(ValidationError, match="Redshift Serverless is no longer supported"):
        RedshiftConfig.model_validate({"workgroup_name": "prod-wg"})


def test_redshift_config_accepts_cluster() -> None:
    cluster_cfg = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    assert cluster_cfg.cluster_identifier == "prod"


def test_redshift_config_disabled_by_default() -> None:
    # Supplementary telemetry connector — opt-in, not on by default.
    assert RedshiftConfig.model_validate({"cluster_identifier": "prod"}).enabled is False


def test_redshift_page_enables_policy_and_driver_health_tabs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.dashboard.views import provider_focus, redshift_focus

    captured: dict[str, object] = {}
    monkeypatch.setattr(redshift_focus, "provider_label", lambda _: "Redshift")
    monkeypatch.setattr(
        provider_focus,
        "render",
        lambda _group, _label, **kwargs: captured.update(kwargs),
    )

    redshift_focus.render()

    assert captured["show_policy"] is True
    assert captured["efficiency_tab"] is redshift_focus._workload_findings_section
    assert captured["efficiency_tab_label"] == "Efficiency & Waste"
    extra_tabs = cast(list[tuple[str, object]], captured["extra_tabs"])
    assert [title for title, _ in extra_tabs] == ["Client Driver Health"]


def test_sync_history_marks_redshift_as_telemetry_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.dashboard.views import connections

    config = RedshiftConfig.model_validate(
        {"name": "Prod", "cluster_identifier": "prod", "enabled": True}
    )
    monkeypatch.setattr(connections, "load_all_connections", lambda _: [config])

    assert connections._telemetry_only_connectors() == {"Prod"}


def test_fetch_driver_health_maps_connection_log_rows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Redshift's STL connection-log pull uses the shared fleet-health contract."""
    from datetime import date
    from unittest.mock import MagicMock

    from flashlight.ingest.base import IngestWindow

    connector = RedshiftConnector(RedshiftConfig.model_validate({"cluster_identifier": "prod"}))

    def execute(_sql: str, *_args: object, name: str) -> list[dict[str, str]]:
        if name == "driver_health":
            return [
                {
                    "charge_month": "2026-07-01",
                    "client_driver": "Redshift JDBC Driver 2.0.0.0",
                    "client_application": "nightly-etl",
                    "executed_by": "svc-etl",
                    "query_count": "44",
                }
            ]
        return []

    execute = MagicMock(side_effect=execute)
    monkeypatch.setattr(connector, "_execute", execute)

    records = list(
        connector.fetch_driver_health(IngestWindow(date(2026, 7, 25), date(2026, 7, 31)))
    )

    assert len(records) == 1
    assert records[0].provider_name == "AWS"
    assert records[0].cluster_id == "prod"
    assert records[0].client_driver == "Redshift JDBC Driver 2.0.0.0"
    assert records[0].query_count == 44
    driver_call = next(
        call for call in execute.call_args_list if call.kwargs["name"] == "driver_health"
    )
    assert "dateadd(day, 1, '2026-07-31')" in driver_call.args[0].lower()
    assert "recordtime >= '2026-07-25'" in driver_call.args[0].lower()


def test_fetch_driver_health_uses_bastion_route_when_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from contextlib import nullcontext
    from datetime import date
    from unittest.mock import MagicMock

    from flashlight.ingest.base import IngestWindow

    connector = RedshiftConnector(
        RedshiftConfig.model_validate(
            {"cluster_identifier": "prod", "db_user": "flashlight", **_BASTION_FIELDS}
        )
    )

    def execute(_sql: str, *_args: object, name: str) -> list[dict[str, str]]:
        return []

    execute = MagicMock(side_effect=execute)
    monkeypatch.setattr(connector, "_execute", execute)
    monkeypatch.setattr(
        connector,
        "_lane_connection_factory",
        lambda _mode: nullcontext(lambda: nullcontext("bastion-connection")),
    )

    window = IngestWindow(date(2026, 7, 25), date(2026, 7, 31))
    assert list(connector.fetch_driver_health(window)) == []
    driver_call = next(
        call for call in execute.call_args_list if call.kwargs["name"] == "driver_health"
    )
    assert driver_call.args[1] == "bastion-connection"


def test_fetch_driver_health_caps_an_oversized_window_to_retained_logs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A historical cost backfill still collects the recent connection-log suffix."""
    from datetime import date
    from unittest.mock import MagicMock

    from flashlight.ingest.base import IngestWindow

    connector = RedshiftConnector(RedshiftConfig.model_validate({"cluster_identifier": "prod"}))
    execute = MagicMock(return_value=[])
    monkeypatch.setattr(connector, "_execute", execute)

    records = list(
        connector.fetch_driver_health(IngestWindow(date(2026, 1, 1), date(2026, 8, 8)))
    )

    assert records == []
    driver_call = next(
        call for call in execute.call_args_list if call.kwargs["name"] == "driver_health"
    )
    assert "recordtime >= '2026-08-02'" in driver_call.args[0].lower()
    assert "dateadd(day, 1, '2026-08-08')" in driver_call.args[0].lower()


def test_fetch_policy_config_maps_control_plane_evidence() -> None:
    from datetime import date
    from unittest.mock import MagicMock

    from flashlight.ingest.base import IngestWindow

    connector = RedshiftConnector(RedshiftConfig.model_validate({"cluster_identifier": "prod"}))
    connector._redshift = MagicMock(
        describe_clusters=MagicMock(
            return_value={
                "Clusters": [
                    {
                        "ClusterIdentifier": "prod",
                        "Encrypted": True,
                        "PubliclyAccessible": False,
                        "EnhancedVpcRouting": True,
                        "AutomatedSnapshotRetentionPeriod": 7,
                        "Tags": [{"Key": "team", "Value": "data"}],
                        "ClusterParameterGroups": [{"ParameterGroupName": "prod-params"}],
                    }
                ]
            }
        ),
        describe_cluster_parameters=MagicMock(
            return_value={
                "Parameters": [{"ParameterName": "require_ssl", "ParameterValue": "true"}]
            }
        ),
    )
    records = list(connector.fetch_policy_config(IngestWindow(date(2026, 7, 1), date(2026, 7, 31))))
    assert len(records) == 1
    assert records[0].encrypted is True
    assert records[0].publicly_accessible is False
    assert records[0].require_ssl is True
    assert records[0].tag_count == 1


def test_redshift_config_aws_profile_defaults_unset() -> None:
    config = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    assert config.aws_profile is None
    # access_key_env/secret_key_env still default (scoped by name/type, since two
    # connections must never silently share one keychain entry — see config.py's
    # scoped_env_name) regardless of aws_profile.
    assert config.access_key_env == "AWS_ACCESS_KEY_ID__REDSHIFT"


def test_redshift_config_accepts_aws_profile() -> None:
    config = RedshiftConfig.model_validate(
        {"cluster_identifier": "prod", "aws_profile": "my-sso-profile"}
    )
    assert config.aws_profile == "my-sso-profile"


def test_bastion_rejects_legacy_workgroup() -> None:
    with pytest.raises(ValidationError, match="Redshift Serverless is no longer supported"):
        RedshiftConfig.model_validate(
            {"workgroup_name": "prod-wg", "db_user": "flashlight_ro", **_BASTION_FIELDS}
        )


def test_bastion_requires_db_user() -> None:
    with pytest.raises(ValidationError, match="db_user"):
        RedshiftConfig.model_validate({"cluster_identifier": "prod", **_BASTION_FIELDS})


def test_bastion_requires_bastion_user() -> None:
    with pytest.raises(ValidationError, match="bastion_user"):
        RedshiftConfig.model_validate(
            {
                "cluster_identifier": "prod",
                "db_user": "flashlight_ro",
                "bastion_host": "bastion.example.com",
                "bastion_private_key_path": "/tmp/bastion-key.pem",
            }
        )


def test_bastion_requires_bastion_private_key_path() -> None:
    with pytest.raises(ValidationError, match="bastion_private_key_path"):
        RedshiftConfig.model_validate(
            {
                "cluster_identifier": "prod",
                "db_user": "flashlight_ro",
                "bastion_host": "bastion.example.com",
                "bastion_user": "ec2-user",
            }
        )


def test_bastion_accepts_valid_config() -> None:
    config = RedshiftConfig.model_validate(
        {"cluster_identifier": "prod", "db_user": "flashlight_ro", **_BASTION_FIELDS}
    )
    assert config.bastion_host == "bastion.example.com"
    assert config.bastion_port == 22  # default


def test_bastion_missing_extra_raises_actionable_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # sshtunnel/redshift_connector aren't in the base install (optional extra) — the
    # connector must fail with a clear "pip install" message, not a bare ImportError.
    # Force ImportError regardless of whether the extra happens to be installed in
    # this dev venv: setting a module to None in sys.modules makes `import x` raise.
    import sys

    monkeypatch.setitem(sys.modules, "sshtunnel", None)
    config = RedshiftConfig.model_validate(
        {"cluster_identifier": "prod", "db_user": "flashlight_ro", **_BASTION_FIELDS}
    )
    connector = RedshiftConnector(config)
    with pytest.raises(ConnectorError, match="redshift-bastion"):
        with connector._bastion_connection():
            pass


def test_bastion_config_not_required_by_default() -> None:
    assert RedshiftConfig.model_validate({"cluster_identifier": "prod"}).bastion_host is None


def test_direct_db_password_env_defaults_unset() -> None:
    config = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    assert config.db_password_env is None


def test_direct_db_password_env_accepts_valid_config() -> None:
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            "db_password_env": "REDSHIFT_DB_PASSWORD",
        }
    )
    assert config.db_password_env == "REDSHIFT_DB_PASSWORD"
    assert config.bastion_host is None  # no tunnel needed for this mode


def test_direct_db_password_env_requires_db_user() -> None:
    with pytest.raises(ValidationError, match="db_user"):
        RedshiftConfig.model_validate(
            {"cluster_identifier": "prod", "db_password_env": "REDSHIFT_DB_PASSWORD"}
        )


def test_direct_db_password_env_rejects_legacy_workgroup() -> None:
    with pytest.raises(ValidationError, match="Redshift Serverless is no longer supported"):
        RedshiftConfig.model_validate(
            {
                "workgroup_name": "prod-wg",
                "db_user": "flashlight_ro",
                "db_password_env": "REDSHIFT_DB_PASSWORD",
            }
        )


def test_db_password_env_allowed_together_with_bastion() -> None:
    # db_password_env just picks how the connection (tunneled or direct)
    # authenticates — it's orthogonal to bastion_host, which picks the connection
    # path. No conflict between the two.
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            "db_password_env": "REDSHIFT_DB_PASSWORD",
            **_BASTION_FIELDS,
        }
    )
    assert config.db_password_env == "REDSHIFT_DB_PASSWORD"
    assert config.bastion_host == "bastion.example.com"


def test_bastion_credentials_uses_static_password_when_set(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_TEST_DB_PASSWORD", "s3cret")
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            **_BASTION_FIELDS,
            "db_password_env": "FLASHLIGHT_TEST_DB_PASSWORD",
        }
    )
    connector = RedshiftConnector(config)
    creds = connector._bastion_credentials()
    assert creds == {"user": "flashlight_ro", "password": "s3cret"}


def test_bastion_credentials_raises_on_empty_password_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("FLASHLIGHT_TEST_DB_PASSWORD", raising=False)
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            **_BASTION_FIELDS,
            "db_password_env": "FLASHLIGHT_TEST_DB_PASSWORD",
        }
    )
    connector = RedshiftConnector(config)
    with pytest.raises(ConnectorError, match="FLASHLIGHT_TEST_DB_PASSWORD"):
        connector._bastion_credentials()


def test_bastion_credentials_falls_back_to_iam_without_password_env() -> None:
    from unittest.mock import MagicMock

    config = RedshiftConfig.model_validate(
        {"cluster_identifier": "prod", "db_user": "flashlight_ro", **_BASTION_FIELDS}
    )
    connector = RedshiftConnector(config)
    connector._redshift = MagicMock(
        get_cluster_credentials=MagicMock(
            return_value={"DbUser": "flashlight_ro", "DbPassword": "temp-iam-password"}
        )
    )
    creds = connector._bastion_credentials()
    assert creds == {"user": "flashlight_ro", "password": "temp-iam-password"}
    connector._redshift.get_cluster_credentials.assert_called_once_with(
        DbUser="flashlight_ro", DbName="dev", ClusterIdentifier="prod", AutoCreate=False
    )


def test_bastion_reuses_one_connection_across_all_queries(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """One SSH tunnel for the entire fetch_efficiency() pull — but, since the
    activity lane and the table-inventory lane run concurrently (see
    _run_lanes/_lane_connection_factory), each of their queries opens its OWN DB
    connection through that one tunnel rather than sharing a single connection.

    The fake cursor reports no rows for anything, including the cheap
    earliest-retained probe — so the pull correctly judges the window
    unmeasurable (no retained history at all) and runs its reduced query set:
    the probe (activity lane, 1 connection), then table-inventory/table-usage/
    table-owner (table lane, 3 concurrent connections) — 4 connections total.
    query-pattern, user-activity, spectrum-table-usage, and the full (expensive)
    cluster_activity query are all skipped — see fetch_efficiency's
    unmeasurable-window guard.
    """
    import sys
    from datetime import date
    from types import ModuleType
    from unittest.mock import MagicMock

    from flashlight.core.settings import get_settings
    from flashlight.ingest.base import IngestWindow

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.description = None
    fake_cursor.fetchall = MagicMock(return_value=[])

    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    fake_redshift_connector = ModuleType("redshift_connector")
    fake_redshift_connector.connect = MagicMock(return_value=fake_conn)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redshift_connector", fake_redshift_connector)

    fake_tunnel = MagicMock()
    fake_tunnel.local_bind_port = 5555
    fake_tunnel.__enter__ = MagicMock(return_value=fake_tunnel)
    fake_tunnel.__exit__ = MagicMock(return_value=False)
    fake_sshtunnel = ModuleType("sshtunnel")
    fake_sshtunnel.SSHTunnelForwarder = MagicMock(return_value=fake_tunnel)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sshtunnel", fake_sshtunnel)

    # _bastion_connection() sets paramiko.DSSKey (a compat shim for sshtunnel
    # 0.4.0) — give it a real attribute to check/set on.
    fake_paramiko = ModuleType("paramiko")
    fake_paramiko.RSAKey = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)

    monkeypatch.setenv("FLASHLIGHT_TEST_DB_PASSWORD", "s3cret")
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            **_BASTION_FIELDS,
            "db_password_env": "FLASHLIGHT_TEST_DB_PASSWORD",
        }
    )
    connector = RedshiftConnector(config)
    connector._redshift = MagicMock(
        describe_clusters=MagicMock(
            return_value={"Clusters": [{"Endpoint": {"Address": "cluster.internal", "Port": 5439}}]}
        ),
        describe_reserved_nodes=MagicMock(return_value={"ReservedNodes": []}),
    )

    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    records = list(connector.fetch_efficiency(window))

    assert len(records) == 1  # just the cluster row — every sub-fetch got 0 rows back
    fake_sshtunnel.SSHTunnelForwarder.assert_called_once()  # one tunnel, shared by every lane
    # 4 connections opened through that one tunnel: 1 for the probe (activity lane)
    # + 3 for table_inventory/table_usage/table_owner (table lane, concurrent).
    assert fake_redshift_connector.connect.call_count == 4
    assert fake_conn.close.call_count == 4
    # probe + table_inventory + table_usage + table_owner — the other 4
    # (cluster_activity, query_patterns, user_activity, spectrum_table_usage) are
    # skipped for an unmeasurable window.
    assert fake_cursor.execute.call_count == 4
    # describe_clusters fires twice total: once to resolve the tunnel endpoint
    # (previously once PER query — 5x — now once for the whole pull) and once,
    # unrelated to this fix, inside _reserved_node_coverage()'s own lookup.
    assert connector._redshift.describe_clusters.call_count == 2


def test_session_init_sql_runs_once_before_the_real_queries(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """RedshiftConfig.session_init_sql (e.g. a WLM ``SET query_group TO '...';``
    to prioritize this connector's queries) must run immediately after EACH
    connection opens and before that connection's own real query — not skipped
    even when the activity window turns out to be unmeasurable (session state,
    unlike the windowed queries, still needs setting up regardless of what the
    window's data looks like). Since fetch_efficiency's activity and
    table-inventory lanes now open one connection per concurrent query (see
    _run_lanes), this runs once per connection (4, for this unmeasurable-window
    fixture) rather than once for the whole pull.
    """
    import sys
    from datetime import date
    from types import ModuleType
    from unittest.mock import MagicMock, call

    from flashlight.core.settings import get_settings
    from flashlight.ingest.base import IngestWindow

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.description = None
    fake_cursor.fetchall = MagicMock(return_value=[])

    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    fake_redshift_connector = ModuleType("redshift_connector")
    fake_redshift_connector.connect = MagicMock(return_value=fake_conn)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redshift_connector", fake_redshift_connector)

    fake_tunnel = MagicMock()
    fake_tunnel.local_bind_port = 5555
    fake_tunnel.__enter__ = MagicMock(return_value=fake_tunnel)
    fake_tunnel.__exit__ = MagicMock(return_value=False)
    fake_sshtunnel = ModuleType("sshtunnel")
    fake_sshtunnel.SSHTunnelForwarder = MagicMock(return_value=fake_tunnel)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sshtunnel", fake_sshtunnel)

    fake_paramiko = ModuleType("paramiko")
    fake_paramiko.RSAKey = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)

    monkeypatch.setenv("FLASHLIGHT_TEST_DB_PASSWORD", "s3cret")
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            **_BASTION_FIELDS,
            "db_password_env": "FLASHLIGHT_TEST_DB_PASSWORD",
            "session_init_sql": "SET query_group TO 'superuser';",
        }
    )
    connector = RedshiftConnector(config)
    connector._redshift = MagicMock(
        describe_clusters=MagicMock(
            return_value={"Clusters": [{"Endpoint": {"Address": "cluster.internal", "Port": 5439}}]}
        ),
        describe_reserved_nodes=MagicMock(return_value={"ReservedNodes": []}),
    )

    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    list(connector.fetch_efficiency(window))

    # 4 connections opened (probe + table_inventory + table_usage + table_owner,
    # across the two concurrent lanes) => session_init_sql runs 4 times, plus the
    # 4 real per-connection queries — 8 execute() calls total. Global ordering
    # across connections isn't deterministic under concurrency (each connection's
    # own session_init always precedes its own real query, but which connection's
    # pair runs first isn't guaranteed) — so assert counts, not a fixed position.
    assert fake_cursor.execute.call_count == 8
    init_call = call("SET query_group TO 'superuser';")
    assert fake_cursor.execute.call_args_list.count(init_call) == 4


def test_session_init_sql_failure_raises_actionable_error(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A typo'd/unset WLM queue name must surface as a clear connector failure,
    not silently abort the connection's transaction and break every query after
    it (the same rollback-worthy failure mode _execute already guards against
    for the real queries)."""
    import sys
    from types import ModuleType
    from unittest.mock import MagicMock

    from flashlight.core.exceptions import ConnectorError

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.execute = MagicMock(side_effect=RuntimeError("no such WLM queue"))

    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    fake_redshift_connector = ModuleType("redshift_connector")
    fake_redshift_connector.connect = MagicMock(return_value=fake_conn)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redshift_connector", fake_redshift_connector)

    monkeypatch.setenv("FLASHLIGHT_TEST_DB_PASSWORD", "s3cret")
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            "db_host": "cluster.internal",
            "db_port": 5439,
            "db_password_env": "FLASHLIGHT_TEST_DB_PASSWORD",
            "session_init_sql": "SET query_group TO 'nonexistent_queue';",
        }
    )
    connector = RedshiftConnector(config)

    with pytest.raises(ConnectorError, match="session_init_sql failed"):
        with connector._direct_connection():
            pass


def test_direct_connection_missing_extra_raises_actionable_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Force ImportError regardless of whether redshift-bastion happens to be
    # installed in this dev venv — see test_bastion_missing_extra_raises_actionable_error.
    import sys

    monkeypatch.setitem(sys.modules, "redshift_connector", None)
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            "db_password_env": "REDSHIFT_DB_PASSWORD",
        }
    )
    connector = RedshiftConnector(config)
    with pytest.raises(ConnectorError, match="redshift-bastion"):
        with connector._direct_connection():
            pass


def test_direct_connection_raises_on_empty_password_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sys
    from types import ModuleType

    monkeypatch.setitem(sys.modules, "redshift_connector", ModuleType("redshift_connector"))
    monkeypatch.delenv("REDSHIFT_DB_PASSWORD", raising=False)
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            "db_password_env": "REDSHIFT_DB_PASSWORD",
        }
    )
    connector = RedshiftConnector(config)
    with pytest.raises(ConnectorError, match="REDSHIFT_DB_PASSWORD"):
        with connector._direct_connection():
            pass


def test_direct_connection_reuses_one_connection_across_all_queries(  # type: ignore[no-untyped-def]
    monkeypatch, tmp_path
) -> None:
    """No tunnel this time — a direct connection straight to the cluster's
    endpoint, resolved once and reused to open one connection per concurrent lane
    query (see test_bastion_reuses_one_connection_across_all_queries for why the
    fake's all-empty cursor means only 4 of the 7 possible queries actually run,
    and why that means 4 connections here too).
    """
    import sys
    from datetime import date
    from types import ModuleType
    from unittest.mock import MagicMock, call

    from flashlight.core.settings import get_settings
    from flashlight.ingest.base import IngestWindow

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.description = None
    fake_cursor.fetchall = MagicMock(return_value=[])

    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    fake_redshift_connector = ModuleType("redshift_connector")
    fake_redshift_connector.connect = MagicMock(return_value=fake_conn)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redshift_connector", fake_redshift_connector)

    monkeypatch.setenv("REDSHIFT_DB_PASSWORD", "s3cret")
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            "db_password_env": "REDSHIFT_DB_PASSWORD",
        }
    )
    connector = RedshiftConnector(config)
    connector._redshift = MagicMock(
        describe_clusters=MagicMock(
            return_value={"Clusters": [{"Endpoint": {"Address": "cluster.internal", "Port": 5439}}]}
        ),
        describe_reserved_nodes=MagicMock(return_value={"ReservedNodes": []}),
    )

    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    records = list(connector.fetch_efficiency(window))

    assert len(records) == 1
    # 4 connections opened (probe + table_inventory + table_usage + table_owner,
    # across the two concurrent lanes), each to the same resolved endpoint —
    # endpoint/password are resolved once and reused by every one of them.
    expected_call = call(
        host="cluster.internal",
        port=5439,
        database="dev",
        user="flashlight_ro",
        password="s3cret",
        ssl=True,
    )
    assert fake_redshift_connector.connect.call_args_list == [expected_call] * 4
    assert fake_conn.close.call_count == 4
    assert fake_cursor.execute.call_count == 4  # probe + table_{inventory,usage,owner}
    assert connector._redshift.describe_clusters.call_count == 2  # endpoint + reserved-nodes


def test_db_host_override_skips_describe_clusters_for_endpoint(  # type: ignore[no-untyped-def]
    monkeypatch, tmp_path
) -> None:
    """db_host/db_port answer 'are we missing the host name for redshift' — an
    explicit endpoint override so describe_clusters isn't needed just to find the
    cluster's own address. _reserved_node_coverage()'s separate node-count lookup
    still calls describe_clusters — that's unrelated to endpoint resolution.
    """
    import sys
    from datetime import date
    from types import ModuleType
    from unittest.mock import MagicMock, call

    from flashlight.core.settings import get_settings
    from flashlight.ingest.base import IngestWindow

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.description = None
    fake_cursor.fetchall = MagicMock(return_value=[])

    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    fake_redshift_connector = ModuleType("redshift_connector")
    fake_redshift_connector.connect = MagicMock(return_value=fake_conn)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redshift_connector", fake_redshift_connector)

    monkeypatch.setenv("REDSHIFT_DB_PASSWORD", "s3cret")
    config = RedshiftConfig.model_validate(
        {
            "cluster_identifier": "prod",
            "db_user": "flashlight_ro",
            "db_password_env": "REDSHIFT_DB_PASSWORD",
            "db_host": "my-cluster.abc123.us-east-1.redshift.amazonaws.com",
            "db_port": 5439,
        }
    )
    connector = RedshiftConnector(config)
    connector._redshift = MagicMock(
        # Still called by _reserved_node_coverage()'s own node-count lookup —
        # unrelated to db_host, which only skips the endpoint-resolution call.
        describe_clusters=MagicMock(return_value={"Clusters": [{"NumberOfNodes": 2}]}),
        describe_reserved_nodes=MagicMock(return_value={"ReservedNodes": []}),
    )

    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    list(connector.fetch_efficiency(window))

    # 4 connections opened (probe + table_inventory + table_usage + table_owner,
    # across the two concurrent lanes), each to the same db_host-overridden
    # endpoint — resolved once and reused by every one of them.
    expected_call = call(
        host="my-cluster.abc123.us-east-1.redshift.amazonaws.com",
        port=5439,
        database="dev",
        user="flashlight_ro",
        password="s3cret",
        ssl=True,
    )
    assert fake_redshift_connector.connect.call_args_list == [expected_call] * 4
    # describe_clusters fires exactly once — from _reserved_node_coverage()'s own,
    # separate node-count lookup (unrelated to this fix and unaffected by db_host).
    # Previously it ALSO fired to resolve the tunnel/direct-connection endpoint;
    # db_host/db_port make that second call unnecessary.
    assert connector._redshift.describe_clusters.call_count == 1


def test_classify_redshift_cost_category() -> None:
    assert _classify_redshift_cost_category("Spectrum data scan", None) == "spectrum_scan"
    assert (
        _classify_redshift_cost_category("Concurrency Scaling usage", None) == "concurrency_scaling"
    )
    assert _classify_redshift_cost_category("Serverless RPU-Hours", None) == "other"
    assert _classify_redshift_cost_category(None, "backup-storage-sku") == "storage"
    assert _classify_redshift_cost_category("Compute node usage", None) == "compute"
    assert _classify_redshift_cost_category("something unrecognized", None) == "other"
    assert _classify_redshift_cost_category(None, None) == "other"


def test_classify_redshift_cost_category_real_billing_text() -> None:
    # AWS's actual compute/scan line items never say "compute"/"node"/"spectrum" —
    # confirmed against a live account's real ChargeDescription text, where these
    # were previously falling through to "other" as the single largest line item.
    assert (
        _classify_redshift_cost_category(
            "USD 1.4181 hourly fee per Redshift, ra3.4xlarge instance", None
        )
        == "compute"
    )


def test_spectrum_cost_allocation_reconciles_to_target_charge() -> None:
    from datetime import date

    from flashlight.efficiency.model import EfficiencyRecord, EntityType

    records = [
        EfficiencyRecord(
            provider_name="AWS",
            charge_month=date(2026, 1, 1),
            entity_type=EntityType.TABLE,
            entity_id="prod:spectrum:lake.events",
            cause_detail={"spectrum_scanned_gb": 80.0},
        ),
        EfficiencyRecord(
            provider_name="AWS",
            charge_month=date(2026, 1, 1),
            entity_type=EntityType.TABLE,
            entity_id="prod:spectrum:lake.clicks",
            cause_detail={"spectrum_scanned_gb": 20.0},
        ),
    ]
    allocated, available = RedshiftConnector._allocate_spectrum_cost(records, 200.0, True)

    assert available is True
    costs = [float(r.cause_detail["spectrum_allocated_cost"]) for r in allocated]
    assert costs == pytest.approx([160.0, 40.0])
    assert sum(costs) == pytest.approx(200.0)


def test_spectrum_cost_allocation_fails_closed_for_partial_window() -> None:
    from datetime import date

    from flashlight.efficiency.model import EfficiencyRecord, EntityType

    records = [
        EfficiencyRecord(
            provider_name="AWS",
            charge_month=date(2026, 1, 1),
            entity_type=EntityType.TABLE,
            entity_id="prod:spectrum:lake.events",
            cause_detail={"spectrum_scanned_gb": 80.0},
        )
    ]
    allocated, available = RedshiftConnector._allocate_spectrum_cost(records, 200.0, False)

    assert available is False
    assert "spectrum_allocated_cost" not in allocated[0].cause_detail
    assert (
        _classify_redshift_cost_category("Redshift, ra3.4xlarge reserved instance applied", None)
        == "compute"
    )
    assert (
        _classify_redshift_cost_category("$5.00 per Terabyte for Redshift Data Scan", None)
        == "spectrum_scan"
    )
    assert (
        _classify_redshift_cost_category(
            "Unused commitment for arn:aws:redshift:us-west-2:1234:reserved-instances/abc", None
        )
        == "committed"
    )


def test_field_value_unwraps_data_api_field_union() -> None:
    assert _field_value({"stringValue": "hello"}) == "hello"
    assert _field_value({"longValue": 42}) == 42
    assert _field_value({"doubleValue": 3.5}) == 3.5
    assert _field_value({"booleanValue": True}) is True
    assert _field_value({"isNull": True}) is None
    assert _field_value({}) is None


def test_connector_constructs_without_live_credentials() -> None:
    # boto3.client() builds a local client object — no network call — so this must
    # not require real AWS credentials to be configured, same expectation as any
    # other connector's __init__.
    config = RedshiftConfig.model_validate({"cluster_identifier": "prod", "region": "us-east-1"})
    connector = RedshiftConnector(config)
    assert connector.name == "redshift"


def test_test_connection_data_api_provisioned() -> None:
    from unittest.mock import MagicMock

    config = RedshiftConfig.model_validate({"cluster_identifier": "prod", "database": "dev"})
    connector = RedshiftConnector(config)
    connector._redshift = MagicMock(
        describe_clusters=MagicMock(
            return_value={"Clusters": [{"Endpoint": {"Address": "prod.example.com", "Port": 5439}}]}
        )
    )
    connector._data = _FakeDataApiClient({"SELECT 1": (["?column?"], [[1]])})

    message = connector.test_connection()

    assert "cluster resolved to prod.example.com:5439" in message
    assert "Data API" in message


def test_cluster_endpoint_wraps_unexpected_exception() -> None:
    from unittest.mock import MagicMock

    config = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    connector = RedshiftConnector(config)
    connector._redshift = MagicMock(describe_clusters=MagicMock(side_effect=Exception("boom")))

    with pytest.raises(ConnectorError):
        connector._cluster_endpoint()


def test_fetch_yields_nothing() -> None:
    from datetime import date

    from flashlight.ingest.base import IngestWindow

    config = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    connector = RedshiftConnector(config)
    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    assert list(connector.fetch(window)) == []


def _field(value: object) -> dict[str, object]:
    """Wrap a plain Python value as a Redshift Data API Field-union dict (test fixture,
    the inverse of redshift.py's own ``_field_value``)."""
    if value is None:
        return {"isNull": True}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"longValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


class _FakeDataApiClient:
    """Routes ``execute_statement`` by a substring unique to each vendored SQL file,
    so one fake instance can stand in for all seven queries fetch_efficiency() runs.
    """

    def __init__(self, responses: dict[str, tuple[list[str], list[list[object]]]]) -> None:
        self._responses = responses  # marker substring -> (columns, rows)

    def execute_statement(self, *, Sql: str, **_: object) -> dict[str, str]:  # noqa: N803
        for marker in self._responses:
            if marker in Sql:
                return {"Id": marker}
        raise AssertionError(f"no fake response registered for SQL: {Sql[:200]}")

    def describe_statement(self, *, Id: str) -> dict[str, str]:  # noqa: N803
        return {"Status": "FINISHED"}

    def get_statement_result(self, *, Id: str, **_: object) -> dict[str, object]:  # noqa: N803
        columns, rows = self._responses[Id]
        return {
            "ColumnMetadata": [{"name": c} for c in columns],
            "Records": [[_field(v) for v in row] for row in rows],
        }


def test_fetch_efficiency_yields_all_entity_types(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """End-to-end fetch_efficiency() with a mocked Data API/Redshift client and real
    BRONZE FOCUS rows (for the cost breakdown, now read from disk instead of a Cost
    Explorer call) — closes the gap that only config validation and downstream
    waste-rule classification were previously tested, not the connector's own
    aggregation.
    """
    from datetime import date, datetime
    from decimal import Decimal
    from unittest.mock import MagicMock

    from flashlight.core.settings import get_settings
    from flashlight.efficiency.model import EfficiencyRecord, EntityType
    from flashlight.focus.enums import ChargeCategory, ServiceCategory
    from flashlight.focus.model import FocusRecord
    from flashlight.ingest.base import IngestWindow
    from flashlight.lake import bronze

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    bronze.write_window(
        "aws_focus",
        window,
        [
            FocusRecord(
                provider_name="AWS",
                billing_account_id="123456789012",
                billing_period_start=date(2026, 1, 1),
                billing_period_end=date(2026, 2, 1),
                charge_period_start=datetime(2026, 1, 15),
                charge_period_end=datetime(2026, 1, 16),
                charge_category=ChargeCategory.USAGE,
                service_category=ServiceCategory.DATABASES,
                service_name="Amazon Redshift",
                effective_cost=Decimal("500.0"),
                resource_id="arn:aws:redshift:us-east-1:123456789012:cluster:prod",
                x_cost_subcategory="compute",
                x_source_connector="aws_focus",
            ),
        ],
        ingest_run_id="test-run",
    )

    config = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    connector = RedshiftConnector(config)

    fake_data = _FakeDataApiClient(
        {
            # The cheap earliest-retained probe fetch_efficiency() runs before the
            # full cluster_activity query below — same underlying fact (retention
            # reaches well before the window), keyed by its own exact SQL text so it
            # can't collide with cluster_activity's "earliest_retained_query_ts"
            # column alias.
            _EARLIEST_RETAINED_SQL: (
                ["earliest_retained_query_ts"],
                [["2025-06-01"]],
            ),
            "wlm_queue_wait_us_p95": (
                [
                    "query_count",
                    "wlm_queue_wait_us_p95",
                    "wlm_queue_wait_us_p99",
                    "wlm_wait_to_exec_ratio",
                    "disk_spill_query_count",
                    "concurrency_scaling_active_seconds",
                    "earliest_retained_query_ts",
                ],
                # earliest_retained_query_ts well before the window start (2026-01-01)
                # — stl_query retains the whole window, so the measured 1000 queries
                # is a real "not idle" signal, not a retention-rolled-off artifact.
                [[1000, 3000.0, 6000.0, 0.5, 20, 120.0, "2025-06-01"]],
            ),
            "qry_md5": (
                [
                    "qry_md5",
                    "sample_query_id",
                    "last_seen_at",
                    "sample_query_text",
                    "sample_query_owner",
                    "run_count",
                    "total_run_min",
                    "avg_exec_min",
                    "avg_queue_min",
                    "pct_runs_spilling",
                    "avg_disk_spill_gb",
                    "avg_workmem_gb",
                    "avg_skew_ratio",
                    "max_skew_ratio",
                    "avg_slices_in_use",
                    "top_user",
                ],
                [
                    [
                        "abc123",
                        7654321,
                        "2026-01-31 23:59:00",
                        "SELECT * FROM orders",
                        "alice",
                        5,
                        42.0,
                        2.0,
                        0.5,
                        0.6,
                        3.5,
                        1.2,
                        4.0,
                        6.0,
                        8.0,
                        "alice",
                    ]
                ],
            ),
            "exec_microseconds": (
                [
                    "username",
                    "query_count",
                    "exec_microseconds",
                    "total_exec_microseconds",
                    "queue_microseconds",
                    "cpu_microseconds",
                    "blocks_read",
                    "temp_blocks_to_disk",
                    "scan_rows",
                    "spectrum_scan_rows",
                    "spectrum_scan_mb",
                    "spill_gb",
                ],
                [
                    [
                        "alice",
                        100,
                        800_000_000,
                        1_000_000_000,
                        10_000_000,
                        400_000_000,
                        5000,
                        10,
                        20000,
                        0,
                        0.0,
                        1.5,
                    ],
                    [
                        "bob",
                        50,
                        200_000_000,
                        1_000_000_000,
                        5_000_000,
                        100_000_000,
                        1000,
                        0,
                        5000,
                        0,
                        0.0,
                        0.0,
                    ],
                ],
            ),
            "svv_table_info": (
                [
                    "table_id",
                    "database",
                    "schema",
                    "table",
                    "encoded",
                    "diststyle",
                    "size",
                    "unsorted",
                    "stats_off",
                    "tbl_rows",
                ],
                [[42, "dev", "public", "orders", "N", "KEY", 10240, 25.0, 30.0, 1_000_000]],
            ),
            "stl_scan": (
                ["table_id", "query_count", "last_access_at"],
                [[42, 3, "2026-01-01 00:00:00"]],
            ),
            # A separate query from svv_table_info, not a JOIN — see _TABLE_OWNER_SQL's
            # own comment on why (pg_tables is leader-node-only).
            "pg_tables": (
                ["schemaname", "tablename", "tableowner"],
                [["public", "orders", "data_eng"]],
            ),
        }
    )
    monkeypatch.setattr(connector, "_data", fake_data)
    monkeypatch.setattr(
        connector,
        "_redshift",
        MagicMock(
            describe_clusters=MagicMock(return_value={"Clusters": [{"NumberOfNodes": 4}]}),
            describe_reserved_nodes=MagicMock(return_value={"ReservedNodes": []}),
        ),
    )
    records = list(connector.fetch_efficiency(window))

    by_type: dict[EntityType, list[EfficiencyRecord]] = {}
    for r in records:
        by_type.setdefault(r.entity_type, []).append(r)

    cluster_rows = by_type[EntityType.SQL_WAREHOUSE]
    assert len(cluster_rows) == 1
    assert cluster_rows[0].cause_detail["wlm_queue_wait_ms_p99"] == pytest.approx(6.0)
    assert float(cluster_rows[0].billed_cost) == pytest.approx(500.0)
    # Window is fully within stl_query's retained range (see earliest_retained_query_ts
    # above) — the measured 1000 queries must come through as a real activity count,
    # not get nulled out by the unmeasurable-window guard.
    assert cluster_rows[0].activity_count == 1000
    assert "activity_window_unmeasurable" not in cluster_rows[0].cause_detail

    pattern_rows = by_type[EntityType.QUERY_PATTERN]
    assert len(pattern_rows) == 1
    assert pattern_rows[0].entity_id == "prod:abc123"
    assert pattern_rows[0].entity_name == "Query 7654321"
    assert pattern_rows[0].owner_user == "alice"
    assert pattern_rows[0].cause_detail["sample_query_id"] == 7654321
    assert pattern_rows[0].cause_detail["sample_query_text"] == "SELECT * FROM orders"
    assert pattern_rows[0].cause_detail["query_owner"] == "alice"
    assert pattern_rows[0].cause_detail["pct_runs_spilling"] == pytest.approx(0.6)

    user_rows = {r.entity_name: r for r in by_type[EntityType.SQL_WAREHOUSE_USER]}
    assert user_rows["alice"].cause_detail["duration_share_pct"] == pytest.approx(80.0)
    assert float(user_rows["alice"].billed_cost) == pytest.approx(400.0)  # 500 * 0.8
    assert user_rows["bob"].cause_detail["duration_share_pct"] == pytest.approx(20.0)

    table_rows = by_type[EntityType.TABLE]
    assert len(table_rows) == 1
    assert table_rows[0].activity_count == 3
    assert table_rows[0].cause_detail["days_since_last_access"] == 30
    # Cluster-prefixed entity_id, same convention as query_pattern/sql_warehouse_user
    # — a bare "dev.public.orders" isn't unique once more than one cluster is
    # instrumented, and the prefix is what lets the dashboard filter per cluster.
    assert table_rows[0].entity_id == "prod:dev.public.orders"
    assert table_rows[0].entity_name == "dev.public.orders"
    # Owner comes from the separate pg_tables query, matched by (schema, table).
    assert table_rows[0].owner_user == "data_eng"


def test_activity_unmeasurable_pure_logic() -> None:
    from datetime import date, datetime

    from flashlight.ingest.connectors.redshift import _activity_unmeasurable, _opt_date

    window_end = date(2026, 1, 31)
    assert _activity_unmeasurable(window_end, None) is True  # no retained history at all
    assert _activity_unmeasurable(window_end, date(2026, 2, 15)) is True  # after window end
    # Retention reaches partway into the window (not back to its start) — real signal
    # for the retained days, not thrown away just because it's not full-window coverage.
    assert _activity_unmeasurable(window_end, date(2026, 1, 15)) is False
    assert _activity_unmeasurable(window_end, date(2025, 12, 1)) is False  # covers whole window
    assert _activity_unmeasurable(window_end, window_end) is False  # exactly at end

    assert _opt_date(None) is None
    assert _opt_date("") is None
    assert _opt_date("2026-01-15") == date(2026, 1, 15)
    assert _opt_date("2026-01-15 08:30:00") == date(2026, 1, 15)  # Data API stringValue
    assert _opt_date(date(2026, 1, 15)) == date(2026, 1, 15)
    assert _opt_date(datetime(2026, 1, 15, 8, 30)) == date(2026, 1, 15)  # bastion driver


def test_activity_window_unmeasurable_when_window_predates_stl_retention(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A window older than what stl_query still retains must not report a confident
    activity_count=0 — count(*) over an empty window reads the same whether the
    cluster was genuinely idle or the log simply rolled off, and only the former is
    honest "idle" waste. Covers both the too-old-window case and the
    no-retained-history-at-all case (earliest_retained_query_ts is NULL).
    """
    from datetime import date

    from flashlight.ingest.base import IngestWindow

    config = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    connector = RedshiftConnector(config)
    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    activity_cols = [
        "query_count",
        "wlm_queue_wait_us_p95",
        "wlm_queue_wait_us_p99",
        "wlm_wait_to_exec_ratio",
        "disk_spill_query_count",
        "concurrency_scaling_active_seconds",
        "earliest_retained_query_ts",
    ]

    for earliest_retained in ("2026-07-01", None):  # too-old window, and no history at all
        row: list[object] = [0, None, None, None, 0, 0.0, earliest_retained]
        fake_data = _FakeDataApiClient({"wlm_queue_wait_us_p95": (activity_cols, [row])})
        monkeypatch.setattr(connector, "_data", fake_data)

        activity = connector._activity(window)
        assert activity["query_count"] is None
        assert activity["disk_spill_query_count"] is None
        assert activity["concurrency_scaling_active_seconds"] is None
        assert activity["activity_window_unmeasurable"] is True


def test_activity_partial_window_keeps_real_counts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """STL_* tables commonly retain only ~1-2 weeks — for a calendar-month ingest
    window that's the normal case, not an edge case, so this must not null out real
    signal just because retention doesn't reach back to the window's start. Only a
    window retention doesn't reach *at all* (test above) gets nulled.
    """
    from datetime import date

    from flashlight.ingest.base import IngestWindow

    config = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    connector = RedshiftConnector(config)
    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    activity_cols = [
        "query_count",
        "wlm_queue_wait_us_p95",
        "wlm_queue_wait_us_p99",
        "wlm_wait_to_exec_ratio",
        "disk_spill_query_count",
        "concurrency_scaling_active_seconds",
        "earliest_retained_query_ts",
    ]
    # Retention only reaches back to Jan 20 (11 of the 31 requested days), but those
    # 11 days have 40 real queries, 3 of which spilled.
    row: list[object] = [40, 3000.0, 6000.0, 0.5, 3, 120.0, "2026-01-20"]
    fake_data = _FakeDataApiClient({"wlm_queue_wait_us_p95": (activity_cols, [row])})
    monkeypatch.setattr(connector, "_data", fake_data)

    activity = connector._activity(window)
    assert activity["query_count"] == 40
    assert activity["disk_spill_query_count"] == 3
    assert activity["concurrency_scaling_active_seconds"] == pytest.approx(120.0)
    assert activity["activity_window_unmeasurable"] is False
    assert activity["activity_measured_since"] == "2026-01-20"


def test_capacity_metrics_capture_cluster_cpu_and_disk_without_claiming_node_precision() -> None:
    """CloudWatch capacity evidence is an hourly cluster summary, independent of WLM."""
    from datetime import UTC, date, datetime
    from unittest.mock import MagicMock

    from flashlight.ingest.base import IngestWindow

    connector = RedshiftConnector(RedshiftConfig.model_validate({"cluster_identifier": "prod"}))
    connector._cloudwatch = MagicMock()
    connector._cloudwatch.get_metric_statistics.side_effect = [
        {
            "Datapoints": [
                {
                    "Timestamp": datetime(2026, 7, 1, 0, tzinfo=UTC),
                    "Average": 20.0,
                    "Maximum": 42.0,
                },
                {
                    "Timestamp": datetime(2026, 7, 2, 0, tzinfo=UTC),
                    "Average": 30.0,
                    "Maximum": 78.0,
                },
            ]
        },
        {
            "Datapoints": [
                {
                    "Timestamp": datetime(2026, 7, 1, 0, tzinfo=UTC),
                    "Average": 45.0,
                    "Maximum": 49.0,
                },
                {
                    "Timestamp": datetime(2026, 7, 2, 0, tzinfo=UTC),
                    "Average": 50.0,
                    "Maximum": 71.0,
                },
            ]
        },
    ]

    window = IngestWindow(date(2026, 7, 1), date(2026, 7, 2))
    metrics = connector._capacity_metrics(window, "prod")

    assert metrics == {
        "cpu_avg_pct": 25.0,
        "cpu_max_pct": 78.0,
        "disk_avg_pct": 47.5,
        "disk_max_pct": 71.0,
        "measured_since": "2026-07-01",
        "measured_until": "2026-07-02",
    }
    first = connector._cloudwatch.get_metric_statistics.call_args_list[0].kwargs
    assert first["MetricName"] == "CPUUtilization"
    assert first["Dimensions"] == [{"Name": "ClusterIdentifier", "Value": "prod"}]
    assert first["Period"] == 2 * 24 * 60 * 60
    assert first["Unit"] == "Percent"


def test_capacity_metrics_permission_failure_is_non_blocking() -> None:
    from datetime import date
    from unittest.mock import MagicMock

    from flashlight.ingest.base import IngestWindow

    connector = RedshiftConnector(RedshiftConfig.model_validate({"cluster_identifier": "prod"}))
    connector._cloudwatch = MagicMock()
    connector._cloudwatch.get_metric_statistics.side_effect = RuntimeError("AccessDenied")

    window = IngestWindow(date(2026, 7, 1), date(2026, 7, 1))
    assert connector._capacity_metrics(window, "prod") == {
        "cpu_avg_pct": None,
        "cpu_max_pct": None,
        "disk_avg_pct": None,
        "disk_max_pct": None,
        "measured_since": None,
        "measured_until": None,
    }


def test_partial_activity_skips_expensive_detail_queries(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A short retained slice still yields cluster-level truth, but not misleading
    per-user/pattern/Spectrum drill-downs that scan broad system views."""
    from contextlib import nullcontext
    from datetime import date

    from flashlight.ingest.base import IngestWindow

    connector = RedshiftConnector(RedshiftConfig.model_validate({"cluster_identifier": "prod"}))
    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    monkeypatch.setattr(connector, "_probe_earliest_retained", lambda _conn: date(2026, 1, 28))
    monkeypatch.setattr(
        connector,
        "_activity",
        lambda _window, _conn, **_kwargs: {
            "query_count": 12,
            "activity_measured_since": "2026-01-28",
            "activity_window_unmeasurable": False,
        },
    )
    for method in ("_fetch_query_patterns", "_fetch_user_activity", "_fetch_spectrum_table_usage"):
        monkeypatch.setattr(
            connector,
            method,
            lambda *_args, **_kwargs: pytest.fail(f"{method} should have been skipped"),
        )

    activity, records = connector._run_activity_lane(
        window, "prod", date(2026, 1, 1), 0.0, lambda: nullcontext(None)
    )

    assert activity["query_count"] == 12
    assert records == []


def test_table_inventory_cache_reuses_catalog_but_keeps_usage_live(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from contextlib import nullcontext
    from datetime import date

    from flashlight.core.settings import get_settings
    from flashlight.ingest.base import IngestWindow

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    connector = RedshiftConnector(RedshiftConfig.model_validate({"cluster_identifier": "prod"}))
    calls: list[str] = []
    results: dict[str, list[dict[str, object]]] = {
        "table_inventory": [
            {
                "table_id": 7,
                "database": "dev",
                "schema": "public",
                "table": "orders",
                "size": 10,
            }
        ],
        "table_usage": [{"table_id": 7, "query_count": 2}],
        "table_owner": [{"schemaname": "public", "tablename": "orders", "tableowner": "owner"}],
    }

    def _execute(_sql: str, _conn: object, *, name: str) -> list[dict[str, object]]:
        calls.append(name)
        return results[name]

    monkeypatch.setattr(connector, "_execute", _execute)
    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))

    def lane() -> AbstractContextManager[Any]:
        return nullcontext(None)

    first = connector._run_table_inventory_lane(window, "prod", date(2026, 1, 1), lane)
    assert len(first) == 1
    assert set(calls) == {"table_inventory", "table_usage", "table_owner"}

    calls.clear()
    second = connector._run_table_inventory_lane(window, "prod", date(2026, 1, 1), lane)
    assert len(second) == 1
    assert calls == ["table_usage"]


def test_detail_sql_scopes_step_views_to_window_query_ids() -> None:
    from flashlight.ingest.connectors import redshift

    patterns = redshift._QUERY_PATTERN_QUERY_PATH.read_text()
    users = redshift._USER_ACTIVITY_QUERY_PATH.read_text()

    assert "top_patterns AS" in patterns
    assert "candidate_queries AS" in patterns
    assert "FROM svl_query_report r\n    JOIN candidate_queries q ON q.query = r.query" in patterns
    user_scope = "FROM svl_query_report r\n    JOIN q ON q.query = r.query AND q.userid = r.userid"
    assert user_scope in users


def test_table_usage_sql_is_bounded_to_the_effective_system_log_window() -> None:
    from flashlight.ingest.connectors import redshift

    sql = redshift._TABLE_USAGE_SQL.replace(":start_date", "'2026-08-02'").replace(
        ":end_date", "'2026-08-09'"
    )

    assert "s.starttime >= '2026-08-02'" in sql
    assert "s.starttime < '2026-08-09'" in sql


def test_execute_rolls_back_shared_connection_after_a_failed_query() -> None:
    """A failed statement leaves a real SQL connection's transaction aborted — without
    a rollback, every later query reusing that same connection fails with "current
    transaction is aborted", not its own error. Regression test for the exact
    cascading failure seen against a live cluster: table_inventory's own bug (see
    _TABLE_OWNER_SQL's separation from _TABLE_INVENTORY_SQL above) poisoned the
    unrelated spectrum_table_usage query that ran right after it, on the one
    connection fetch_efficiency() reuses for its whole pull.
    """
    from unittest.mock import MagicMock

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.execute = MagicMock(side_effect=Exception("boom"))
    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    config = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    connector = RedshiftConnector(config)
    with pytest.raises(ConnectorError):
        connector._execute("SELECT 1", fake_conn, name="whatever")

    fake_conn.rollback.assert_called_once()


def test_user_activity_share_uses_true_total_even_when_top_n_excludes_a_user() -> None:
    """Regression test for the top_n cap fix: duration_share_pct must reflect each
    returned user's share of the FULL cluster total (via the SQL's window-function
    total_exec_microseconds column), not just the sum of the rows that made the cap.
    A user excluded by LIMIT still consumed real exec time — pretending the visible
    rows are the whole cluster would overstate everyone else's share.
    """
    from datetime import date

    from flashlight.ingest.base import IngestWindow

    config = RedshiftConfig.model_validate({"cluster_identifier": "prod"})
    connector = RedshiftConnector(config)

    # carol (100M microseconds) is excluded from the returned rows (as if the SQL's
    # LIMIT :top_n cut her), but total_exec_microseconds (1.1B) still counts her.
    fake_data = _FakeDataApiClient(
        {
            "exec_microseconds": (
                [
                    "username",
                    "query_count",
                    "exec_microseconds",
                    "total_exec_microseconds",
                    "queue_microseconds",
                    "cpu_microseconds",
                    "blocks_read",
                    "temp_blocks_to_disk",
                    "scan_rows",
                    "spectrum_scan_rows",
                    "spectrum_scan_mb",
                    "spill_gb",
                ],
                [
                    ["alice", 100, 800_000_000, 1_100_000_000, 0, 0, 0, 0, 0, 0, 0.0, 0.0],
                    ["bob", 50, 200_000_000, 1_100_000_000, 0, 0, 0, 0, 0, 0, 0.0, 0.0],
                ],
            ),
        }
    )
    connector._data = fake_data

    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    records = {
        r.entity_name: r
        for r in connector._fetch_user_activity(window, "prod", date(2026, 1, 1), 500.0)
    }

    assert len(records) == 2  # only the two returned rows, carol never appears
    # 100 * 800M / 1.1B, NOT 100 * 800M / (800M+200M) — the latter would wrongly give 80%
    assert records["alice"].cause_detail["duration_share_pct"] == pytest.approx(
        100 * 800_000_000 / 1_100_000_000
    )
    assert records["bob"].cause_detail["duration_share_pct"] == pytest.approx(
        100 * 200_000_000 / 1_100_000_000
    )
    # The two visible shares don't sum to 100% — the gap is carol's excluded share.
    total_visible_share = (
        records["alice"].cause_detail["duration_share_pct"]
        + records["bob"].cause_detail["duration_share_pct"]
    )
    assert total_visible_share < 100.0


def test_dashboard_waste_query_includes_shared_categories_on_redshift_entities(  # type: ignore[no-untyped-def]
    tmp_path, monkeypatch
) -> None:
    """The Redshift tab's waste query must surface ``idle``/
    ``sql_warehouse_user_concentration`` when they fire on a Redshift entity
    (``sql_warehouse``/``sql_warehouse_user`` under provider AWS is Redshift-only —
    S3's own signal uses ``entity_type='storage'``), while still excluding an AWS
    finding that isn't Redshift's (e.g. an idle S3 bucket).
    """
    from datetime import date
    from decimal import Decimal

    from flashlight.core.settings import get_settings
    from flashlight.dashboard.data import gold_df
    from flashlight.efficiency.model import EfficiencyRecord, EntityType
    from flashlight.ingest.base import IngestWindow
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    month = date(2026, 6, 1)
    window = IngestWindow(date(2026, 6, 1), date(2026, 6, 30))
    records = [
        # Idle Redshift cluster — shared `idle` category, entity_type=sql_warehouse.
        EfficiencyRecord(
            provider_name="AWS",
            charge_month=month,
            entity_type=EntityType.SQL_WAREHOUSE,
            entity_id="redshift-idle",
            billed_cost=Decimal("100"),
            activity_count=0,
            x_source_connector="redshift",
        ),
        # One user drives a Redshift warehouse — shared `sql_warehouse_user_concentration`.
        EfficiencyRecord(
            provider_name="AWS",
            charge_month=month,
            entity_type=EntityType.SQL_WAREHOUSE_USER,
            entity_id="redshift-user-alice",
            billed_cost=Decimal("100"),
            cause_detail={"duration_share_pct": 80.0, "query_count": 500},
            x_source_connector="redshift",
        ),
        # Redshift-prefixed, priced category — the pre-existing LIKE match.
        EfficiencyRecord(
            provider_name="AWS",
            charge_month=month,
            entity_type=EntityType.TABLE,
            entity_id="redshift-unused-table",
            billed_cost=Decimal("0"),
            native_quantity=10240.0,
            cause_detail={"days_since_last_access": 120},
            x_source_connector="redshift",
        ),
        # Idle S3 bucket — same shared `idle` category, but NOT Redshift.
        EfficiencyRecord(
            provider_name="AWS",
            charge_month=month,
            entity_type=EntityType.STORAGE,
            entity_id="s3-idle-bucket",
            billed_cost=Decimal("50"),
            activity_count=0,
            x_source_connector="aws_focus",
        ),
    ]
    metrics.write_efficiency(window, records)
    build_gold()

    rows = gold_df(
        "SELECT entity_id, waste_category FROM efficiency.waste_record "
        "WHERE provider_name = 'AWS' AND recoverable_cost > 0 "
        "AND (waste_category LIKE 'redshift_%' "
        "OR entity_type IN ('sql_warehouse', 'sql_warehouse_user')) "
        "ORDER BY entity_id"
    )
    entity_ids = set(rows["entity_id"])
    assert entity_ids == {"redshift-idle", "redshift-user-alice", "redshift-unused-table"}
    assert "s3-idle-bucket" not in entity_ids


def test_rule_coverage_rows_distinguishes_fired_clean_and_no_data() -> None:
    """Pure logic behind the "Optimization rule coverage" table: a category with a
    matching waste_record row is "fired" (priced or not); one whose entity_type was
    measured but never matched is "clean"; one whose entity_type was never measured
    this window is "no data" — the distinction the redshift efficiency retention fix
    exists to make possible (see redshift.py's _activity_unmeasurable).
    """
    import pandas as pd

    from flashlight.dashboard.views.efficiency_waste import rule_coverage_rows
    from flashlight.efficiency.waste_rules import coverage_groups

    records = pd.DataFrame(
        [
            {
                "waste_category": "redshift_spectrum_scan_cost",
                "recoverable_cost": 1038.0,
                "detail": "Spectrum scan $3458 this month — verify partition pruning",
            },
            {
                "waste_category": "redshift_disk_spill_queries",
                "recoverable_cost": 0.0,
                "detail": "34 of 812 queries spilled to disk",
            },
            {
                "waste_category": "redshift_table_unused",
                "recoverable_cost": 500.0,
                "detail": "not queried in 212 days",
            },
            {
                "waste_category": "redshift_table_unused",
                "recoverable_cost": 300.0,
                "detail": "not queried in 145 days",
            },
        ]
    )
    # sql_warehouse and table were both measured this window; query_pattern's own
    # telemetry pull came back completely empty (the real gap seen in production).
    measured_types = {"sql_warehouse", "table"}

    # The rule→group structure is derived from the pool now (coverage_groups) rather than
    # restated in the view, so this passes AWS's own set — the same rules the old
    # hand-maintained map listed, minus the Databricks-only ones it could never fire.
    by_category = {
        r["category"]: r
        for r in rule_coverage_rows(records, measured_types, coverage_groups("AWS"))
    }

    # A single fired entity states what actually fired, not just a count — this is
    # what tells a reader "$1,038 recoverable, for what" right in the coverage row.
    fired_priced = by_category["redshift_spectrum_scan_cost"]
    assert fired_priced["Status"] == (
        "fired · Spectrum scan $3458 this month — verify partition pruning"
    )
    assert fired_priced["Recoverable"] == pytest.approx(1038.0)

    fired_unpriced = by_category["redshift_disk_spill_queries"]
    assert fired_unpriced["Status"] == "fired · 34 of 812 queries spilled to disk (unpriced)"
    assert pd.isna(fired_unpriced["Recoverable"])

    # Two entities matched the same category — aggregated into one row, not two, with
    # the highest-recoverable entity's own detail surfaced as a representative example.
    fired_multi = by_category["redshift_table_unused"]
    assert fired_multi["Status"] == "fired · 2 entities — e.g. not queried in 212 days"
    assert fired_multi["Recoverable"] == pytest.approx(800.0)

    # idle (sql_warehouse) never matched, but sql_warehouse WAS measured → clean.
    clean = by_category["idle"]
    assert clean["Status"] == "clean"
    assert pd.isna(clean["Recoverable"])

    # query_pattern rules: entity_type never appears in measured_types → no data.
    no_data = by_category["redshift_query_pattern_skew"]
    assert no_data["Status"] == "no data"
    assert pd.isna(no_data["Recoverable"])


def test_cluster_facets_split_instrumented_from_cost_only(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Two Redshift clusters bill on this AWS account; only one has a `redshift`
    connector entry configured (so only it shows up in efficiency_entity_month).
    _cost_cluster_ids() must see both — that's what lets _waste_section() list the
    uninstrumented one instead of silently omitting it — while _telemetry_cluster_ids()
    sees only the one with real optimization telemetry.

    Named "Prod" here, not "redshift": a real multi-cluster setup names each
    connection (effective_connector_name), so x_source_connector is never the
    literal connector type once more than one cluster is configured — the
    matching must not assume otherwise (see redshift_focus.py's own reasoning).
    """
    from datetime import date, datetime
    from decimal import Decimal

    from flashlight.core.settings import get_settings
    from flashlight.dashboard.views import redshift_focus
    from flashlight.efficiency.model import EfficiencyRecord, EntityType
    from flashlight.focus.enums import ChargeCategory, ServiceCategory
    from flashlight.focus.model import FocusRecord
    from flashlight.ingest.base import IngestWindow
    from flashlight.lake import bronze, metrics
    from flashlight.transform.runner import build_gold

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    bronze.write_window(
        "aws_focus",
        window,
        [
            FocusRecord(
                provider_name="AWS",
                billing_account_id="123456789012",
                billing_period_start=date(2026, 1, 1),
                billing_period_end=date(2026, 2, 1),
                charge_period_start=datetime(2026, 1, 15),
                charge_period_end=datetime(2026, 1, 16),
                charge_category=ChargeCategory.USAGE,
                service_category=ServiceCategory.DATABASES,
                service_name="Amazon Redshift",
                effective_cost=Decimal("500.0"),
                resource_id=f"arn:aws:redshift:us-east-1:123456789012:cluster:{cluster}",
                x_source_connector="aws_focus",
            )
            for cluster in ("instrumented-cluster", "cost-only-cluster")
        ],
        ingest_run_id="test-run",
    )
    metrics.write_efficiency(
        window,
        [
            EfficiencyRecord(
                provider_name="AWS",
                charge_month=date(2026, 1, 1),
                entity_type=EntityType.SQL_WAREHOUSE,
                entity_id="instrumented-cluster",
                billed_cost=Decimal("100"),
                activity_count=5,
                x_source_connector="Prod",
            ),
        ],
    )
    build_gold()

    assert redshift_focus._cost_cluster_ids() == {"instrumented-cluster", "cost-only-cluster"}
    assert redshift_focus._telemetry_cluster_ids() == ["instrumented-cluster"]


def test_redshift_cluster_cost_view_keeps_components_and_unassigned_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Attribution landing grain is the billed cluster, not its redundant service.

    Invoice components remain below that cluster: this is what prevents a
    query-duration user allocation from silently assigning storage or Spectrum spend.
    A resource-less Redshift line is a real bill line, so it must stay as an explicit
    unassigned bucket rather than being guessed onto one configured connector.
    """
    from datetime import date, datetime
    from decimal import Decimal

    from flashlight.core.settings import get_settings
    from flashlight.dashboard.data import gold_df
    from flashlight.focus.enums import ChargeCategory, ServiceCategory
    from flashlight.focus.model import FocusRecord
    from flashlight.ingest.base import IngestWindow
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))

    def _row(
        cost: str, component: str | None, *, service: str = "Amazon Redshift", resource: str | None
    ) -> FocusRecord:
        return FocusRecord(
            provider_name="AWS",
            billing_account_id="123456789012",
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 2, 1),
            charge_period_start=datetime(2026, 1, 15),
            charge_period_end=datetime(2026, 1, 16),
            charge_category=ChargeCategory.USAGE,
            service_category=ServiceCategory.DATABASES,
            service_name=service,
            effective_cost=Decimal(cost),
            resource_id=resource,
            x_cost_subcategory=component,
            x_source_connector="aws_focus",
        )

    cluster_arn = "arn:aws:redshift:us-east-1:123456789012:cluster:prod"
    bronze.write_window(
        "aws_focus",
        window,
        [
            _row("100", "compute", resource=cluster_arn),
            _row("30", "storage", resource=cluster_arn),
            _row("10", "concurrency_scaling", resource=cluster_arn),
            _row("20", "spectrum_scan", service="Amazon Redshift Spectrum", resource=cluster_arn),
            _row("5", None, resource=None),
        ],
        ingest_run_id="test-run",
    )
    build_gold()

    rows = gold_df(
        'SELECT cluster_id, cost_subcategory, gross_cost FROM "aws".redshift_cluster_cost_month '
        "ORDER BY cluster_id, cost_subcategory"
    )
    observed = {(r.cluster_id, r.cost_subcategory): float(r.gross_cost) for r in rows.itertuples()}
    assert observed == {
        ("prod", "compute"): 100.0,
        ("prod", "concurrency_scaling"): 10.0,
        ("prod", "spectrum_scan"): 20.0,
        ("prod", "storage"): 30.0,
        ("(not assigned to a cluster)", "(unclassified)"): 5.0,
    }


def test_table_inventory_carries_compute_weighted_scan_evidence() -> None:
    """A table workload score splits query execution across scanned tables, not bills."""
    from datetime import date

    from flashlight.ingest.base import IngestWindow

    connector = RedshiftConnector(RedshiftConfig.model_validate({"cluster_identifier": "prod"}))
    records = list(
        connector._build_table_inventory_records(  # noqa: SLF001 - output contract test
            IngestWindow(date(2026, 1, 1), date(2026, 1, 31)),
            "prod",
            date(2026, 1, 1),
            [
                {
                    "table_id": 42,
                    "database": "warehouse",
                    "schema": "analytics",
                    "table": "events",
                    "size": 1024,
                    "encoded": "Y",
                    "diststyle": "KEY(events_id)",
                    "unsorted": 0,
                    "stats_off": 0,
                    "tbl_rows": 100,
                }
            ],
            [
                {
                    "table_id": 42,
                    "query_count": 3,
                    "scan_bytes": 1024**3,
                    "rows_pre_filter": 1000,
                    "rows_returned": 100,
                    "weighted_exec_seconds": 30.0,
                }
            ],
            [{"schemaname": "analytics", "tablename": "events", "tableowner": "data"}],
        )
    )
    assert len(records) == 1
    record = records[0]
    assert record.entity_id == "prod:warehouse.analytics.events"
    assert record.cause_detail["table_weighted_exec_seconds"] == pytest.approx(30.0)
    assert record.cause_detail["table_compute_share_pct"] == pytest.approx(100.0)
    assert record.cause_detail["table_scan_gb"] == pytest.approx(1.0)
