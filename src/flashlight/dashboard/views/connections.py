"""Connections — add/edit data sources and trigger a sync, no CLI required.

Reads/writes ``connections.yml`` directly (:mod:`flashlight.ingest.config`) and
triggers ``flashlight ingest`` as a subprocess (:mod:`flashlight.dashboard.
ingest_runner`) rather than calling the ingest runner in-process, so the
dashboard process itself stays a read-only reader of GOLD — same "ingest is
the sole writer" boundary as the CLI, just launched by a button instead of a
terminal. Secrets never touch ``connections.yml``; see
:mod:`flashlight.dashboard.connection_credentials`.

One small dedicated form-builder per connector type below rather than a
generic schema-driven form generator — three known, finite field sets don't
need one.

Cost (AWS) and Redshift-cluster connections are separate connector types (one
account-wide cost pull typically backs several Redshift clusters — see
``AwsFocusConfig``/``RedshiftConfig`` in ``ingest/config.py``), but the "Data
sources" list below groups every Redshift entry under its AWS cost source so
the two read as one picture instead of an unrelated flat list.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

import pandas as pd
from nicegui import ui
from pydantic import BaseModel, ValidationError

from flashlight import scaffold
from flashlight.dashboard import chrome
from flashlight.dashboard.connection_credentials import save_secret
from flashlight.dashboard.ingest_runner import stream_sync
from flashlight.ingest.config import (
    AwsFocusConfig,
    DatabricksConfig,
    RedshiftConfig,
    effective_connector_name,
    load_all_connections,
    load_connections,
    save_connections,
    scoped_env_name,
)
from flashlight.lake import paths
from flashlight.lake.runlog import read_runs

_TYPE_LABELS: dict[str, str] = {
    "aws_focus": "AWS cost source",
    "databricks": "Databricks",
    "redshift": "Redshift usage",
}

# Matches the progress printer's own "  {name} ... {rows:,} rows done" / "  {name}
# ... failed" lines (cli.py's _progress_printer) — not its "  {name} ..." start
# line, which has nothing after "...". Used to tick the sync dialog's "N of M
# connectors done" counter as the live tail streams in.
_CONNECTOR_DONE_RE = re.compile(r"^\s*.+ \.\.\. (?:[\d,]+ rows done|failed)\s*$")

Collector = Callable[[], tuple[BaseModel, dict[str, str]] | None]


def _text(label: str, value: str = "") -> ui.input:
    return ui.input(label, value=value).classes("w-full")


def _half(label: str, value: str = "") -> ui.input:
    """A field meant to sit beside another inside a `with ui.row().classes("w-full
    gap-3"):` block, so a long form reads as a grid instead of a wall of single
    stacked inputs."""
    return ui.input(label, value=value).classes("flex-1 min-w-0")


def _subheading(label: str) -> None:
    ui.label(label).classes("text-sm font-medium mt-2").style(f"color:{chrome.INK_SECONDARY}")


def _checkbox(label: str, value: bool = False) -> ui.checkbox:
    return ui.checkbox(label, value=value)


def _secret(label: str) -> ui.input:
    return (
        ui.input(label, placeholder="leave blank to keep the current value")
        .props("type=password")
        .classes("w-full")
    )


def _secret_half(label: str) -> ui.input:
    """A `_secret` field meant to sit beside another (see `_half`)."""
    return (
        ui.input(label, placeholder="leave blank to keep the current value")
        .props("type=password")
        .classes("flex-1 min-w-0")
    )


def _aws_focus_form(existing: BaseModel | None) -> Collector:
    existing = existing if isinstance(existing, AwsFocusConfig) else None
    name = _text('Name (e.g. "Prod")', existing.name or "" if existing else "")
    cost_source = ui.select(
        {"focus_export": "FOCUS export (S3) — recommended", "cost_explorer": "Cost Explorer"},
        value=existing.cost_source if existing else "focus_export",
        label="Cost source",
    ).classes("w-full")
    with ui.row().classes("w-full gap-3") as s3_fields:
        bucket = _half("S3 bucket", existing.s3_bucket or "" if existing else "")
        prefix = _half("S3 prefix", existing.s3_prefix if existing else "")
    s3_fields.bind_visibility_from(cost_source, "value", backward=lambda v: v == "focus_export")
    region = _text("Region", existing.region if existing else "us-east-1")
    profile = _text(
        "AWS profile (optional — takes priority over the keys below)",
        (existing.aws_profile or "") if existing else "",
    )
    with ui.row().classes("w-full gap-3"):
        access_key = _secret_half("AWS Access Key ID (optional)")
        secret_key = _secret_half("AWS Secret Access Key")

    def collect() -> tuple[BaseModel, dict[str, str]] | None:
        try:
            cfg = AwsFocusConfig(
                name=name.value or None,
                cost_source=cost_source.value,
                s3_bucket=bucket.value or None,
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
    name = _text('Name (e.g. "Prod workspace")', existing.name or "" if existing else "")
    host = _text("Workspace host (https://...)", existing.host if existing else "")
    warehouse = _text(
        "SQL warehouse ID (optional)", existing.sql_warehouse_id or "" if existing else ""
    )
    token = _secret("Databricks personal access token")

    def collect() -> tuple[BaseModel, dict[str, str]] | None:
        try:
            cfg = DatabricksConfig(
                name=name.value or None, host=host.value, sql_warehouse_id=warehouse.value or None
            )
        except ValidationError as exc:
            ui.notify(str(exc), type="negative")
            return None
        secrets = {cfg.token_env: token.value} if token.value else {}
        return cfg, secrets

    return collect


def _redshift_form(existing: BaseModel | None) -> Collector:
    existing = existing if isinstance(existing, RedshiftConfig) else None
    name = _text('Name (e.g. "Prod (main)")', existing.name or "" if existing else "")

    _subheading("Cluster")
    with ui.row().classes("w-full gap-3"):
        cluster_id = _half(
            "Cluster identifier (or use a workgroup)",
            existing.cluster_identifier or "" if existing else "",
        )
        workgroup = _half(
            "Serverless workgroup (or use a cluster)",
            existing.workgroup_name or "" if existing else "",
        )
    with ui.row().classes("w-full gap-3"):
        database = _half("Database", existing.database if existing else "dev")
        region = _half("Region", existing.region if existing else "us-east-1")
    with ui.row().classes("w-full gap-3"):
        db_host = _half(
            "DB host override (auto-discovered if blank)",
            existing.db_host or "" if existing else "",
        )
        db_port = _half(
            "DB port override (optional)",
            str(existing.db_port) if existing and existing.db_port else "",
        )

    _subheading("AWS auth (Data API / describe_clusters)")
    profile = _text(
        "AWS profile (optional — takes priority over the keys below)",
        existing.aws_profile or "" if existing else "",
    )
    with ui.row().classes("w-full gap-3"):
        access_key = _secret_half("AWS Access Key ID (optional)")
        secret_key = _secret_half("AWS Secret Access Key")

    _subheading("DB auth (pick one — default: IAM temp credentials)")
    with ui.row().classes("w-full gap-3"):
        db_user = _half(
            "DB user (IAM, or paired with a password below)",
            existing.db_user or "" if existing else "",
        )
        secret_arn = _half(
            "Secrets Manager ARN (alternative to DB user)",
            existing.secret_arn or "" if existing else "",
        )
    # The env var NAME a password/passphrase already resolves to is preserved across
    # edits/duplicates even though the secret VALUE input below always starts blank —
    # otherwise editing (or duplicating) a connection without retyping the secret
    # would silently drop it.
    db_password_env_name = existing.db_password_env if existing else None
    db_password = _secret("DB password (native auth, optional — else IAM temp credentials)")

    with ui.expansion(
        "Bastion / SSH tunnel — optional, only if the cluster isn't reachable directly"
    ).classes("w-full"):
        with ui.row().classes("w-full gap-3"):
            bastion_host = _half("Bastion host", existing.bastion_host or "" if existing else "")
            bastion_port = _half("Bastion port", str(existing.bastion_port) if existing else "22")
        bastion_user = _text("Bastion SSH user", existing.bastion_user or "" if existing else "")
        bastion_key_path = _text(
            "Bastion private key path", existing.bastion_private_key_path or "" if existing else ""
        )
        bastion_passphrase_env_name = (
            existing.bastion_private_key_passphrase_env if existing else None
        )
        bastion_passphrase = _secret(
            "Bastion key passphrase (optional — only if the key itself is passphrase-protected)"
        )

    def collect() -> tuple[BaseModel, dict[str, str]] | None:
        # Scoped by this connection's own (currently-typed) name, not a fixed
        # shared constant — so two Redshift connections never default to the
        # same keychain entry for their password/passphrase.
        db_password_env = db_password_env_name or (
            scoped_env_name(
                "FLASHLIGHT_REDSHIFT_DB_PASSWORD", name=name.value or None, ctype="redshift"
            )
            if db_password.value
            else None
        )
        bastion_passphrase_env = bastion_passphrase_env_name or (
            scoped_env_name(
                "FLASHLIGHT_REDSHIFT_BASTION_PASSPHRASE", name=name.value or None, ctype="redshift"
            )
            if bastion_passphrase.value
            else None
        )
        try:
            cfg = RedshiftConfig(
                name=name.value or None,
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
                db_password_env=db_password_env,
                bastion_private_key_passphrase_env=bastion_passphrase_env,
            )
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative")
            return None
        secrets = {}
        if access_key.value:
            secrets[cfg.access_key_env] = access_key.value
        if secret_key.value:
            secrets[cfg.secret_key_env] = secret_key.value
        if db_password.value and db_password_env:
            secrets[db_password_env] = db_password.value
        if bastion_passphrase.value and bastion_passphrase_env:
            secrets[bastion_passphrase_env] = bastion_passphrase.value
        return cfg, secrets

    return collect


_FORM_BUILDERS: dict[str, Callable[[BaseModel | None], Collector]] = {
    "aws_focus": _aws_focus_form,
    "databricks": _databricks_form,
    "redshift": _redshift_form,
}


def _summary(cfg: BaseModel) -> str:
    if isinstance(cfg, AwsFocusConfig):
        if cfg.cost_source == "cost_explorer":
            return "Cost Explorer"
        return f"s3://{cfg.s3_bucket}/{cfg.s3_prefix}".rstrip("/")
    if isinstance(cfg, DatabricksConfig):
        return cfg.host
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

        aws_entries = [
            (i, c) for i, c in enumerate(all_connections) if isinstance(c, AwsFocusConfig)
        ]
        redshift_entries = [
            (i, c) for i, c in enumerate(all_connections) if isinstance(c, RedshiftConfig)
        ]
        databricks_entries = [
            (i, c) for i, c in enumerate(all_connections) if isinstance(c, DatabricksConfig)
        ]

        def _row_content(i: int, cfg: BaseModel, *, sub: bool = False) -> None:
            ctype: str = getattr(cfg, "type")  # noqa: B009 - always present, 3 known config classes
            cfg_enabled: bool = getattr(cfg, "enabled")
            cfg_name = effective_connector_name(cfg)
            with ui.row().classes("w-full items-center justify-between py-2"):
                with ui.column().classes("gap-0.5 pl-6" if sub else "gap-0.5"):
                    label_classes = "text-sm font-medium" if sub else "text-base font-semibold"
                    ui.label(f"{_TYPE_LABELS.get(ctype, ctype)}: {cfg_name}").classes(
                        label_classes
                    ).style(f"color:{chrome.INK_PRIMARY}")
                    ui.label(_summary(cfg)).classes("text-xs").style(f"color:{chrome.INK_MUTED}")
                with ui.row().classes("items-center gap-2"):
                    badge_color = chrome.OPPORTUNITY if cfg_enabled else chrome.INK_MUTED
                    ui.label("Enabled" if cfg_enabled else "Disabled").classes("text-xs").style(
                        f"color:{badge_color}"
                    )
                    sync_row_button = ui.button(icon="sync").props("flat dense round").tooltip(
                        f"Sync {cfg_name}"
                    )
                    sync_row_button.on_click(
                        lambda cfg_name=cfg_name, b=sync_row_button: _sync(b, cfg_name)
                    )
                    if not cfg_enabled:
                        sync_row_button.disable()
                    ui.button(
                        icon="content_copy",
                        on_click=lambda cfg=cfg: _open_dialog(
                            None,
                            cfg.model_copy(update={"name": f"{cfg_name} (copy)"}),
                            all_connections,
                        ),
                    ).props("flat dense round").tooltip("Duplicate")
                    ui.button(
                        icon="edit",
                        on_click=lambda cfg=cfg, i=i: _open_dialog(i, cfg, all_connections),
                    ).props("flat dense round")
                    ui.button(
                        icon="delete",
                        on_click=lambda i=i: _delete(i, all_connections),
                    ).props("flat dense round")

        def _row(i: int, cfg: BaseModel) -> None:
            with chrome.panel():
                _row_content(i, cfg)

        def _grouped_card(
            aws_row: tuple[int, BaseModel], redshift_rows: Sequence[tuple[int, BaseModel]]
        ) -> None:
            """One card for the AWS cost source and every Redshift cluster it feeds —
            a divider between sub-rows, not separate cards, since they aren't
            independent: this is the one cost pull backing all of these clusters.
            """
            i, aws_cfg = aws_row
            with chrome.panel():
                _row_content(i, aws_cfg)
                for ri, r in redshift_rows:
                    ui.separator().style(f"background:{chrome.BORDER}")
                    _row_content(ri, r, sub=True)

        if aws_entries:
            _grouped_card(aws_entries[0], redshift_entries)
            for i, aws_cfg in aws_entries[1:]:
                _row(i, aws_cfg)
        else:
            for i, r_cfg in redshift_entries:
                _row(i, r_cfg)
        for i, db_cfg in databricks_entries:
            _row(i, db_cfg)

    def _delete(index: int, all_connections: list[BaseModel]) -> None:
        updated = [c for j, c in enumerate(all_connections) if j != index]
        save_connections(updated)
        connections_body.refresh()

    def _open_dialog(
        existing_index: int | None, prefill: BaseModel | None, all_connections: list[BaseModel]
    ) -> None:
        type_key: str = getattr(prefill, "type") if prefill else "aws_focus"  # noqa: B009
        with ui.dialog() as dialog, ui.card().classes("gap-3 p-5").style(
            "width:640px; max-width:92vw; max-height:85vh; overflow-y:auto;"
        ):
            ui.label("Edit connection" if existing_index is not None else "Add connection").classes(
                "text-base font-semibold"
            ).style(f"color:{chrome.INK_PRIMARY}")

            type_select = ui.select(_TYPE_LABELS, value=type_key, label="Type").classes("w-full")
            if existing_index is not None:
                type_select.disable()
            enabled_checkbox = _checkbox(
                "Enabled", getattr(prefill, "enabled") if prefill else True  # noqa: B009
            )

            form_area = ui.column().classes("w-full gap-2")
            collector: list[Collector] = []

            def _rebuild_form() -> None:
                form_area.clear()
                collector.clear()
                with form_area:
                    collector.append(_FORM_BUILDERS[type_select.value](prefill))

            if existing_index is None:
                type_select.on_value_change(lambda _: _rebuild_form())
            _rebuild_form()

            def _save() -> None:
                result = collector[0]()
                if result is None:
                    return
                cfg, secrets = result
                setattr(cfg, "enabled", enabled_checkbox.value)  # noqa: B010 - all 3 config classes have it
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
                try:
                    save_connections(updated)
                except Exception as exc:  # noqa: BLE001 - surface e.g. duplicate-name errors
                    ui.notify(str(exc), type="negative")
                    return
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

    browser_tz = {"name": "UTC"}

    with chrome.panel():
        with ui.row().classes("w-full items-center justify-between"):
            chrome.panel_title("Recent sync history")
            connector_filter = ui.select(["All"], value="All")

        @ui.refreshable
        def history_body() -> None:
            df = read_runs().drop(columns=["run_id"])
            if df.empty:
                chrome.section_caption("No syncs yet.")
                return
            connector_filter.set_options(["All", *sorted(df["connector"].unique())])
            if connector_filter.value != "All":
                df = df[df["connector"] == connector_filter.value]
            for col in ("started_at", "finished_at"):
                df[col] = (
                    pd.to_datetime(df[col]).dt.tz_convert(browser_tz["name"]).dt.strftime(
                        "%Y-%m-%d %H:%M %Z"
                    )
                )
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

    connector_filter.on_value_change(history_body.refresh)

    async def _detect_browser_tz() -> None:
        try:
            browser_tz["name"] = await ui.run_javascript(
                "Intl.DateTimeFormat().resolvedOptions().timeZone"
            )
        except TimeoutError:
            return
        history_body.refresh()

    ui.timer(0.1, _detect_browser_tz, once=True)

    async def _sync(button: ui.button, connector: str | None = None) -> None:
        """Runs both "Sync now" (``connector=None``, every enabled connector) and
        each row's own Sync button (``connector=<its effective name>``) — same
        subprocess call, same "Full refresh" checkbox.

        The output dialog opens immediately and tails the subprocess live (via
        :func:`stream_sync`) instead of showing a bare spinner and dumping
        everything at the end — a sync can run for minutes, and "is it doing
        anything?" was the whole complaint. A "N of M connectors done" counter
        (parsed from the same progress lines the tail already shows) and a
        "Download log" button (the accumulated text, client-side — no server
        route or on-disk log file needed) ride along for free.
        """
        total = 1 if connector is not None else len(load_connections(str(paths.connections_path())))
        lines: list[str] = []
        done = 0

        with ui.dialog() as log_dialog, ui.card().style("width:700px; max-width:95vw;"):
            ui.label(f"Syncing {connector or 'all connections'}...").classes(
                "text-sm font-semibold"
            ).style(f"color:{chrome.INK_PRIMARY}")
            progress_label = ui.label(f"0 / {total} connectors done").classes("text-xs").style(
                f"color:{chrome.INK_SECONDARY}"
            )
            log_widget = ui.log(max_lines=2000).classes("w-full").style(
                "height:50vh; font-size:12px;"
            )
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(
                    "Download log",
                    icon="download",
                    on_click=lambda: ui.download(
                        "\n".join(lines).encode(),
                        f"flashlight-sync-{connector or 'all'}.log",
                    ),
                ).props("flat no-caps")
                ui.button("Close", on_click=log_dialog.close).props("flat no-caps")
        log_dialog.open()

        def _on_line(line: str) -> None:
            nonlocal done
            lines.append(line)
            log_widget.push(line)
            if _CONNECTOR_DONE_RE.match(line):
                done += 1
                progress_label.set_text(f"{done} / {total} connectors done")

        button.props("loading")
        try:
            returncode = await stream_sync(
                paths.connections_path(),
                _on_line,
                full_refresh=full_refresh_checkbox.value,
                connector=connector,
            )
        except Exception as exc:  # noqa: BLE001 - surface a launch failure in the dialog, not a crash
            _on_line(f"sync failed to start: {exc}")
            returncode = 1
        finally:
            button.props(remove="loading")

        progress_label.set_text(f"{done} / {total} connectors done — exit code {returncode}")
        ui.notify(
            "Sync completed" if returncode == 0 else "Sync failed — see output above",
            type="positive" if returncode == 0 else "negative",
        )
        history_body.refresh()

    sync_button.on_click(lambda: _sync(sync_button))
