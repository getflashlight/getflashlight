"""Connections — add/edit data sources and trigger a sync, no CLI required.

Reads/writes ``connections.yml`` directly (:mod:`flashlight.ingest.config`) and
triggers ``flashlight ingest`` as a subprocess (:mod:`flashlight.dashboard.
ingest_runner`) rather than calling the ingest runner in-process, so the
dashboard process itself stays a read-only reader of GOLD — same "ingest is
the sole writer" boundary as the CLI, just launched by a button instead of a
terminal. Secrets never touch ``connections.yml``; see
:mod:`flashlight.dashboard.connection_credentials`.

One small dedicated form-builder per connector type below rather than a
generic schema-driven form generator — five known, finite field sets don't
need one.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd
from nicegui import run, ui
from pydantic import BaseModel, ValidationError

from flashlight import scaffold
from flashlight.dashboard import chrome
from flashlight.dashboard.connection_credentials import save_secret
from flashlight.dashboard.ingest_runner import sync_now
from flashlight.ingest.config import (
    AwsFocusConfig,
    AwsInfraConfig,
    DatabricksConfig,
    FocusFileConfig,
    RedshiftConfig,
    load_all_connections,
    save_connections,
)
from flashlight.lake import paths
from flashlight.lake.runlog import read_runs

_TYPE_LABELS: dict[str, str] = {
    "focus_file": "FOCUS file (local CSV/Parquet)",
    "aws_focus": "AWS FOCUS export (S3)",
    "databricks": "Databricks",
    "aws_infra": "AWS Infra (Cost Explorer fallback)",
    "redshift": "Redshift (efficiency telemetry)",
}

# Fixed synthetic env var names for the two Redshift secret fields that have no
# built-in default (unlike token_env/access_key_env/secret_key_env) — a UI-entered
# value needs *some* name to store/inject under. Hand-edit connections.yml for a
# different name.
_REDSHIFT_DB_PASSWORD_ENV = "FLASHLIGHT_REDSHIFT_DB_PASSWORD"
_REDSHIFT_BASTION_PASSPHRASE_ENV = "FLASHLIGHT_REDSHIFT_BASTION_PASSPHRASE"

Collector = Callable[[], tuple[BaseModel, dict[str, str]] | None]


def _text(label: str, value: str = "") -> ui.input:
    return ui.input(label, value=value).classes("w-full")


def _checkbox(label: str, value: bool = False) -> ui.checkbox:
    return ui.checkbox(label, value=value)


def _secret(label: str) -> ui.input:
    return (
        ui.input(label, placeholder="leave blank to keep the current value")
        .props("type=password")
        .classes("w-full")
    )


def _focus_file_form(existing: BaseModel | None) -> Collector:
    existing = existing if isinstance(existing, FocusFileConfig) else None
    path = _text("Local FOCUS file path", existing.path if existing else "")
    respect_window = _checkbox(
        "Respect ingest window (off = ingest every row, useful for a backfill file)",
        existing.respect_window if existing else False,
    )

    def collect() -> tuple[BaseModel, dict[str, str]] | None:
        try:
            cfg = FocusFileConfig(path=path.value, respect_window=respect_window.value)
        except ValidationError as exc:
            ui.notify(str(exc), type="negative")
            return None
        return cfg, {}

    return collect


def _aws_focus_form(existing: BaseModel | None) -> Collector:
    existing = existing if isinstance(existing, AwsFocusConfig) else None
    bucket = _text("S3 bucket", existing.s3_bucket if existing else "")
    prefix = _text("S3 prefix", existing.s3_prefix if existing else "")
    region = _text("Region", existing.region if existing else "us-east-1")
    profile = _text(
        "AWS profile (optional — takes priority over the keys below)",
        (existing.aws_profile or "") if existing else "",
    )
    access_key = _secret("AWS Access Key ID (optional — else profile/IAM role/ambient env)")
    secret_key = _secret("AWS Secret Access Key")

    def collect() -> tuple[BaseModel, dict[str, str]] | None:
        try:
            cfg = AwsFocusConfig(
                s3_bucket=bucket.value,
                s3_prefix=prefix.value,
                region=region.value or "us-east-1",
                aws_profile=profile.value or None,
            )
        except ValidationError as exc:
            ui.notify(str(exc), type="negative")
            return None
        secrets = {}
        if access_key.value:
            secrets[cfg.access_key_env] = access_key.value
        if secret_key.value:
            secrets[cfg.secret_key_env] = secret_key.value
        return cfg, secrets

    return collect


def _databricks_form(existing: BaseModel | None) -> Collector:
    existing = existing if isinstance(existing, DatabricksConfig) else None
    host = _text("Workspace host (https://...)", existing.host if existing else "")
    warehouse = _text(
        "SQL warehouse ID (optional)", existing.sql_warehouse_id or "" if existing else ""
    )
    token = _secret("Databricks personal access token")

    def collect() -> tuple[BaseModel, dict[str, str]] | None:
        try:
            cfg = DatabricksConfig(host=host.value, sql_warehouse_id=warehouse.value or None)
        except ValidationError as exc:
            ui.notify(str(exc), type="negative")
            return None
        secrets = {cfg.token_env: token.value} if token.value else {}
        return cfg, secrets

    return collect


def _aws_infra_form(existing: BaseModel | None) -> Collector:
    existing = existing if isinstance(existing, AwsInfraConfig) else None
    region = _text("Region", existing.region if existing else "us-east-1")
    cluster_tag_key = _text(
        "Cluster tag key", existing.cluster_tag_key if existing else "ClusterId"
    )
    default_filters = existing.tag_filters if existing else {"Vendor": "Databricks"}
    tag_filters_text = _text(
        "Tag filters (key=value, comma-separated)",
        ",".join(f"{k}={v}" for k, v in default_filters.items()),
    )
    access_key = _secret("AWS Access Key ID (optional — else profile/IAM role/ambient env)")
    secret_key = _secret("AWS Secret Access Key")

    def collect() -> tuple[BaseModel, dict[str, str]] | None:
        tag_filters: dict[str, str] = {}
        for pair in tag_filters_text.value.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                ui.notify(f"Bad tag filter {pair!r} — expected key=value", type="negative")
                return None
            k, v = pair.split("=", 1)
            tag_filters[k.strip()] = v.strip()
        try:
            cfg = AwsInfraConfig(
                region=region.value or "us-east-1",
                cluster_tag_key=cluster_tag_key.value or "ClusterId",
                tag_filters=tag_filters or {"Vendor": "Databricks"},
            )
        except ValidationError as exc:
            ui.notify(str(exc), type="negative")
            return None
        secrets = {}
        if access_key.value:
            secrets[cfg.access_key_env] = access_key.value
        if secret_key.value:
            secrets[cfg.secret_key_env] = secret_key.value
        return cfg, secrets

    return collect


def _redshift_form(existing: BaseModel | None) -> Collector:
    existing = existing if isinstance(existing, RedshiftConfig) else None
    cluster_id = _text(
        "Cluster identifier (leave blank if using a workgroup)",
        existing.cluster_identifier or "" if existing else "",
    )
    workgroup = _text(
        "Serverless workgroup name (leave blank if using a cluster)",
        existing.workgroup_name or "" if existing else "",
    )
    database = _text("Database", existing.database if existing else "dev")
    db_user = _text("DB user (IAM/Data-API)", existing.db_user or "" if existing else "")
    secret_arn = _text(
        "Secrets Manager secret ARN (optional)", existing.secret_arn or "" if existing else ""
    )
    region = _text("Region", existing.region if existing else "us-east-1")
    profile = _text("AWS profile (optional)", existing.aws_profile or "" if existing else "")
    db_host = _text("DB host override (optional)", existing.db_host or "" if existing else "")
    db_port = _text(
        "DB port override (optional)",
        str(existing.db_port) if existing and existing.db_port else "",
    )
    ui.label("SSH bastion (optional — only for a cluster reachable via SSH tunnel)").classes(
        "text-xs mt-1"
    ).style(f"color:{chrome.INK_MUTED}")
    bastion_host = _text("Bastion host", existing.bastion_host or "" if existing else "")
    bastion_port = _text("Bastion port", str(existing.bastion_port) if existing else "22")
    bastion_user = _text("Bastion SSH user", existing.bastion_user or "" if existing else "")
    bastion_key_path = _text(
        "Bastion private key path", existing.bastion_private_key_path or "" if existing else ""
    )
    access_key = _secret("AWS Access Key ID (optional — else profile/IAM role/ambient env)")
    secret_key = _secret("AWS Secret Access Key")
    db_password = _secret("DB password (native auth, optional — else IAM temp credentials)")
    bastion_passphrase = _secret("Bastion key passphrase (optional)")

    def collect() -> tuple[BaseModel, dict[str, str]] | None:
        try:
            cfg = RedshiftConfig(
                cluster_identifier=cluster_id.value or None,
                workgroup_name=workgroup.value or None,
                database=database.value or "dev",
                db_user=db_user.value or None,
                secret_arn=secret_arn.value or None,
                region=region.value or "us-east-1",
                aws_profile=profile.value or None,
                db_host=db_host.value or None,
                db_port=int(db_port.value) if db_port.value else None,
                bastion_host=bastion_host.value or None,
                bastion_port=int(bastion_port.value) if bastion_port.value else 22,
                bastion_user=bastion_user.value or None,
                bastion_private_key_path=bastion_key_path.value or None,
                db_password_env=_REDSHIFT_DB_PASSWORD_ENV if db_password.value else None,
                bastion_private_key_passphrase_env=(
                    _REDSHIFT_BASTION_PASSPHRASE_ENV if bastion_passphrase.value else None
                ),
            )
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative")
            return None
        secrets = {}
        if access_key.value:
            secrets[cfg.access_key_env] = access_key.value
        if secret_key.value:
            secrets[cfg.secret_key_env] = secret_key.value
        if db_password.value:
            secrets[_REDSHIFT_DB_PASSWORD_ENV] = db_password.value
        if bastion_passphrase.value:
            secrets[_REDSHIFT_BASTION_PASSPHRASE_ENV] = bastion_passphrase.value
        return cfg, secrets

    return collect


_FORM_BUILDERS: dict[str, Callable[[BaseModel | None], Collector]] = {
    "focus_file": _focus_file_form,
    "aws_focus": _aws_focus_form,
    "databricks": _databricks_form,
    "aws_infra": _aws_infra_form,
    "redshift": _redshift_form,
}


def _summary(cfg: BaseModel) -> str:
    if isinstance(cfg, FocusFileConfig):
        return cfg.path
    if isinstance(cfg, AwsFocusConfig):
        return f"s3://{cfg.s3_bucket}/{cfg.s3_prefix}".rstrip("/")
    if isinstance(cfg, DatabricksConfig):
        return cfg.host
    if isinstance(cfg, AwsInfraConfig):
        return f"{cfg.region} · tag {cfg.cluster_tag_key}"
    if isinstance(cfg, RedshiftConfig):
        return cfg.cluster_identifier or cfg.workgroup_name or ""
    return ""


def render() -> None:
    if not paths.connections_path().exists():
        scaffold.scaffold()

    chrome.section_title("Connections")
    chrome.section_caption(
        "Add data sources and sync billing data — no CLI required. Secrets are "
        "stored in your OS keychain, never written to connections.yml."
    )

    @ui.refreshable
    def connections_body() -> None:
        all_connections = load_all_connections(str(paths.connections_path()))
        if not all_connections:
            chrome.section_caption("No connections yet — add one below.")
            return
        for i, cfg in enumerate(all_connections):
            ctype: str = getattr(cfg, "type")  # noqa: B009 - always present, 5 known config classes
            cfg_enabled: bool = getattr(cfg, "enabled")
            with chrome.panel():
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0"):
                        ui.label(_TYPE_LABELS.get(ctype, ctype)).classes(
                            "text-sm font-medium"
                        ).style(f"color:{chrome.INK_PRIMARY}")
                        ui.label(_summary(cfg)).classes("text-xs").style(f"color:{chrome.INK_MUTED}")
                    with ui.row().classes("items-center gap-2"):
                        badge_color = chrome.OPPORTUNITY if cfg_enabled else chrome.INK_MUTED
                        ui.label("Enabled" if cfg_enabled else "Disabled").classes("text-xs").style(
                            f"color:{badge_color}"
                        )
                        ui.button(
                            icon="edit",
                            on_click=lambda cfg=cfg, i=i: _open_dialog(cfg, i, all_connections),
                        ).props("flat dense round")
                        ui.button(
                            icon="delete",
                            on_click=lambda i=i: _delete(i, all_connections),
                        ).props("flat dense round")

    def _delete(index: int, all_connections: list[BaseModel]) -> None:
        updated = [c for j, c in enumerate(all_connections) if j != index]
        save_connections(updated)
        connections_body.refresh()

    def _open_dialog(
        existing: BaseModel | None, existing_index: int | None, all_connections: list[BaseModel]
    ) -> None:
        type_key: str = getattr(existing, "type") if existing else "focus_file"  # noqa: B009
        with ui.dialog() as dialog, ui.card().classes("gap-3 p-5").style(
            "width:520px; max-width:90vw;"
        ):
            ui.label("Edit connection" if existing else "Add connection").classes(
                "text-base font-semibold"
            ).style(f"color:{chrome.INK_PRIMARY}")

            type_select = ui.select(_TYPE_LABELS, value=type_key, label="Type").classes("w-full")
            if existing is not None:
                type_select.disable()
            enabled_checkbox = _checkbox(
                "Enabled", getattr(existing, "enabled") if existing else True  # noqa: B009
            )

            form_area = ui.column().classes("w-full gap-2")
            collector: list[Collector] = []

            def _rebuild_form() -> None:
                form_area.clear()
                collector.clear()
                with form_area:
                    collector.append(_FORM_BUILDERS[type_select.value](existing))

            if existing is None:
                type_select.on_value_change(lambda _: _rebuild_form())
            _rebuild_form()

            def _save() -> None:
                result = collector[0]()
                if result is None:
                    return
                cfg, secrets = result
                setattr(cfg, "enabled", enabled_checkbox.value)  # noqa: B010 - all 5 config classes have it
                for env_name, value in secrets.items():
                    if not save_secret(env_name, value):
                        ui.notify(
                            f"Couldn't reach your OS keychain — {env_name} won't persist.",
                            type="warning",
                        )
                updated = list(all_connections)
                if existing_index is None:
                    updated.append(cfg)
                else:
                    updated[existing_index] = cfg
                save_connections(updated)
                dialog.close()
                connections_body.refresh()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
                ui.button("Save", on_click=_save).props("flat no-caps color=primary")
        dialog.open()

    with ui.row().classes("w-full items-center justify-between"):
        ui.label("Data sources").classes("text-sm font-medium").style(
            f"color:{chrome.INK_SECONDARY}"
        )
        ui.button(
            "Add connection",
            icon="add",
            on_click=lambda: _open_dialog(
                None, None, load_all_connections(str(paths.connections_path()))
            ),
        ).props("flat no-caps color=primary")

    connections_body()

    chrome.section_title("Sync")
    with ui.row().classes("w-full items-center gap-3"):
        full_refresh_checkbox = ui.checkbox(
            "Full refresh (wipe & re-pull each connector's entire history — "
            "use after a config change)"
        )
        sync_button = ui.button("Sync now", icon="sync")

    connector_filter = ui.select(["All"], value="All")

    @ui.refreshable
    def history_body() -> None:
        with chrome.panel():
            with ui.row().classes("w-full items-center justify-between"):
                chrome.panel_title("Recent sync history")
                connector_filter.move()
            df = read_runs().drop(columns=["run_id"])
            if df.empty:
                chrome.section_caption("No syncs yet.")
                return
            connector_filter.set_options(["All", *sorted(df["connector"].unique())])
            if connector_filter.value != "All":
                df = df[df["connector"] == connector_filter.value]
            df["started_at"] = pd.to_datetime(df["started_at"]).dt.strftime("%Y-%m-%d %H:%M UTC")
            df["finished_at"] = pd.to_datetime(df["finished_at"]).dt.strftime("%Y-%m-%d %H:%M UTC")
            chrome.flat_table(
                df,
                key="ingest_runs",
                int_cols=["rows"],
                rename={
                    "connector": "Connector",
                    "status": "Status",
                    "rows": "Rows",
                    "detail": "Detail",
                    "started_at": "Started",
                    "finished_at": "Finished",
                },
            )

    history_body()

    async def _sync() -> None:
        sync_button.props("loading")
        try:
            result = await run.io_bound(
                sync_now, paths.connections_path(), full_refresh=full_refresh_checkbox.value
            )
        finally:
            sync_button.props(remove="loading")
        if result is None:
            ui.notify("Sync didn't run — try again", type="negative")
            return
        ui.notify(
            "Sync completed" if result.returncode == 0 else "Sync failed — see output below",
            type="positive" if result.returncode == 0 else "negative",
        )
        with ui.dialog() as log_dialog, ui.card().style("width:700px; max-width:95vw;"):
            ui.label("Sync output").classes("text-sm font-semibold").style(
                f"color:{chrome.INK_PRIMARY}"
            )
            ui.code((result.stdout or "") + (result.stderr or "")).classes("w-full").style(
                "max-height:50vh; overflow:auto;"
            )
            ui.button("Close", on_click=log_dialog.close).props("flat no-caps")
        log_dialog.open()
        history_body.refresh()

    sync_button.on_click(_sync)
