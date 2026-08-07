"""Connections — add/edit data sources and trigger a sync, no CLI required.

Reads/writes ``connections.yml`` directly (:mod:`flashlight.ingest.config`) and
triggers ``flashlight ingest`` as a subprocess (:mod:`flashlight.dashboard.
ingest_runner`) rather than calling the ingest runner in-process, so the
dashboard process itself stays a read-only reader of GOLD — same "ingest is
the sole writer" boundary as the CLI, just launched by a button instead of a
terminal. Secrets never touch ``connections.yml``; see
:mod:`flashlight.ingest.connection_credentials`.

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

import asyncio
import os
import re
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import pandas as pd
from nicegui import run, ui
from pydantic import BaseModel, ValidationError

from flashlight import scaffold
from flashlight.dashboard import chrome, ingest_runner
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
from flashlight.ingest.connection_credentials import load_secret, save_secret

# test_connection() is read-only (describe_clusters/GetWorkgroup + a throwaway
# SELECT 1) — it doesn't cross the "ingest is the sole writer" boundary above,
# so calling it in-process (unlike a real sync) is fine.
from flashlight.ingest.connectors.redshift import RedshiftConnector
from flashlight.lake import paths
from flashlight.lake.runlog import read_run_groups, read_runs

_TYPE_LABELS: dict[str, str] = {
    "aws_focus": "AWS cost source",
    "databricks": "Databricks",
    "redshift": "Redshift usage",
}

_TYPE_ICONS: dict[str, str] = {
    "aws_focus": "cloud",
    "databricks": "hub",
    "redshift": "storage",
}

# Matches the progress printer's own "  {name} ... {rows:,} rows done" / "  {name}
# ... failed" lines (cli.py's _progress_printer) — not its "  {name} ..." start
# line, which has nothing after "...". Used to tick the sync dialog's "N of M cost
# pulls done" counter as the live tail streams in.
#
# This line only fires after a connector's fetch() (the BRONZE/cost pull) —
# run_ingest() (ingest/runner.py) runs _run_efficiency()/_run_driver_health()/
# build_gold() afterward. _run_efficiency now reports its own per-connector
# "  {name} ... efficiency: N records" / "  {name} ... efficiency failed" lines
# (visible in the tail below, same as any other line) so a connector whose
# fetch() is a no-op — Redshift, whose cost already flows through aws_focus,
# while fetch_efficiency() does the real, often much slower work — doesn't read
# as silently finished the moment this counter ticks. Those lines deliberately
# don't match this regex (they're not "N rows done"/bare "failed"), so this
# counter stays scoped to cost pulls, as the label says. _run_driver_health()
# still doesn't report through the progress callback — same gap, smaller blast
# radius (one connector today).
_CONNECTOR_DONE_RE = re.compile(r"^\s*.+ \.\.\. (?:[\d,]+ rows done|failed)\s*$")

# Redshift's own connect-level timeout (redshift.py's _DB_CONNECT_TIMEOUT_SECS) bounds
# a single socket connect, but a Test connection click can chain several steps
# (describe_clusters, an SSH tunnel, the DB connect, a Data API poll) — this is the
# overall ceiling so the button never spins forever regardless of which step is slow.
_TEST_CONNECTION_TIMEOUT_SECS = 30

Collector = Callable[[], tuple[BaseModel, dict[str, str]] | None]


def _with_hint(field: ui.input, hint: str | None) -> ui.input:
    """Quasar's own below-field caption — for behavior/defaults that don't fit in
    a short label, instead of stuffing them into the label where they wrap or
    truncate in a half-width column."""
    return field.props(f'hint="{hint}"') if hint else field


def _text(
    label: str, value: str = "", *, hint: str | None = None, placeholder: str = ""
) -> ui.input:
    field = ui.input(label, value=value, placeholder=placeholder).classes("w-full")
    return _with_hint(field, hint)


def _half(label: str, value: str = "", *, hint: str | None = None) -> ui.input:
    """A field meant to sit beside another inside a `with ui.row().classes("w-full
    gap-3"):` block, so a long form reads as a grid instead of a wall of single
    stacked inputs."""
    field = ui.input(label, value=value).classes("flex-1 min-w-0")
    return _with_hint(field, hint)


def _subheading(label: str, caption: str | None = None) -> None:
    ui.label(label).classes("text-sm font-medium mt-2").style(f"color:{chrome.INK_SECONDARY}")
    if caption:
        ui.label(caption).classes("text-xs -mt-2").style(f"color:{chrome.INK_MUTED}")


def _checkbox(label: str, value: bool = False) -> ui.checkbox:
    return ui.checkbox(label, value=value)


def _secret_hint(configured: bool, hint: str | None) -> str | None:
    # The real value is never read back into the browser — only whether the keychain
    # already has one under this field's env var name. Quasar's `placeholder` only
    # renders once a field's label floats out of the way (on focus or with content),
    # so an empty secret field's placeholder would stay invisible until clicked into —
    # the always-visible `hint` caption (same mechanism every other field here uses)
    # says it instead, without forcing the label into its floated/shrunk style.
    return "Already saved — leave blank to keep." if configured else hint


def _secret(label: str, *, hint: str | None = None, configured: bool = False) -> ui.input:
    field = ui.input(label).props("type=password").classes("w-full")
    return _with_hint(field, _secret_hint(configured, hint))


def _secret_half(label: str, *, configured: bool = False) -> ui.input:
    """A `_secret` field meant to sit beside another (see `_half`)."""
    field = ui.input(label).props("type=password").classes("flex-1 min-w-0")
    return _with_hint(field, _secret_hint(configured, None))


def _aws_focus_form(existing: BaseModel | None) -> Collector:
    existing = existing if isinstance(existing, AwsFocusConfig) else None
    name = _text("Display name", existing.name or "" if existing else "", placeholder="Prod")
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
        "AWS profile",
        (existing.aws_profile or "") if existing else "",
        hint="Optional — takes priority over the access keys below.",
    )
    with ui.row().classes("w-full gap-3"):
        access_key = _secret_half(
            "Access key ID", configured=bool(existing and load_secret(existing.access_key_env))
        )
        secret_key = _secret_half(
            "Secret access key",
            configured=bool(existing and load_secret(existing.secret_key_env)),
        )

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
    name = _text(
        "Display name", existing.name or "" if existing else "", placeholder="Prod workspace"
    )
    host = _text("Workspace host", existing.host if existing else "", placeholder="https://...")
    warehouse = _text(
        "SQL warehouse ID",
        existing.sql_warehouse_id or "" if existing else "",
        hint="Optional.",
    )
    token = _secret(
        "Databricks personal access token",
        configured=bool(existing and load_secret(existing.token_env)),
    )

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
    name = _text("Display name", existing.name or "" if existing else "", placeholder="Prod (main)")

    with ui.tabs().classes("w-full") as tabs:
        tab_general = ui.tab("General").props("no-caps")
        tab_aws = ui.tab("AWS settings").props("no-caps")
        tab_ssh = ui.tab("SSH tunnel").props("no-caps")
    with ui.tab_panels(tabs, value=tab_general).classes("w-full").style("background:transparent;"):
        with ui.tab_panel(tab_general):
            # Naming mirrors DataGrip's own "Connection type" control (Default / IAM
            # cluster-region / URL only — we have no URL-only equivalent). RedshiftConfig
            # itself picks the runtime path by which of these is set: bastion_host set ->
            # SSH tunnel, elif db_password_env set -> direct SQL (both = "Default" here,
            # a host-based connection), else -> the Data API (see redshift.py's
            # fetch_efficiency mode dispatch; "IAM cluster/region" here, no host needed).
            # "Default" is also DataGrip's own pre-selected option, so a new connection
            # defaults to it here too, even though RedshiftConfig's own wire default
            # (bastion_host/db_password_env both unset) is the Data API path.
            connection_mode = (
                ui.toggle(
                    {"direct": "Default", "data_api": "IAM cluster/region"},
                    value=(
                        "data_api"
                        if existing and not (existing.bastion_host or existing.db_password_env)
                        else "direct"
                    ),
                )
                .props("no-caps")
                .classes("w-full")
            )
            ui.label("").classes("text-xs -mt-1").style(f"color:{chrome.INK_MUTED}").bind_text_from(
                connection_mode,
                "value",
                backward=lambda v: (
                    "No host needed — resolved via the Data API using the cluster/region "
                    "on the AWS settings tab."
                    if v == "data_api"
                    else "Connects directly to the host below."
                ),
            )

            with ui.column().classes("w-full gap-2") as direct_fields:
                with ui.row().classes("w-full gap-3"):
                    db_host = _half("Host", existing.db_host or "" if existing else "")
                    db_port = _half(
                        "Port", str(existing.db_port) if existing and existing.db_port else ""
                    )
            direct_fields.bind_visibility_from(
                connection_mode, "value", backward=lambda v: v == "direct"
            )

            db_user = _text("Database user", existing.db_user or "" if existing else "")

            with ui.column().classes("w-full gap-2") as direct_password_fields:
                # The env var NAME a password already resolves to is preserved across
                # edits/duplicates even though the secret VALUE input below always
                # starts blank — otherwise editing (or duplicating) a connection
                # without retyping the secret would silently drop it.
                db_password_env_name = existing.db_password_env if existing else None
                db_password = _secret(
                    "Database password",
                    configured=bool(db_password_env_name and load_secret(db_password_env_name)),
                )
            direct_password_fields.bind_visibility_from(
                connection_mode, "value", backward=lambda v: v == "direct"
            )

            with ui.column().classes("w-full gap-2") as data_api_fields:
                secret_arn = _text(
                    "Secrets Manager ARN",
                    existing.secret_arn or "" if existing else "",
                    hint="Alternative to the database user above, for Data API auth.",
                )
            data_api_fields.bind_visibility_from(
                connection_mode, "value", backward=lambda v: v == "data_api"
            )

            database = _text(
                "Database name",
                existing.database or "" if existing else "",
                hint='A new Redshift cluster\'s default database is named "dev".',
            )

        with ui.tab_panel(tab_aws):
            # cluster_identifier/workgroup_name + region are required in every
            # connection mode above — they're the entity Redshift telemetry is
            # measured against (describe_clusters, reserved-node coverage), not
            # something only the Data API path needs — grouped here with the AWS
            # credentials that resolve them, since both are "which AWS resource, and
            # how do I authenticate to AWS" rather than "how do I connect to SQL".
            _subheading("Cluster", "The cluster or workgroup this connection measures.")
            cluster_type = (
                ui.toggle(
                    {"provisioned": "Provisioned cluster", "serverless": "Serverless workgroup"},
                    value="serverless" if existing and existing.workgroup_name else "provisioned",
                )
                .props("no-caps")
                .classes("w-full")
            )
            with ui.row().classes("w-full gap-3"):
                cluster_id = _half(
                    "Cluster identifier", existing.cluster_identifier or "" if existing else ""
                )
                workgroup = _half(
                    "Workgroup name", existing.workgroup_name or "" if existing else ""
                )
                region = _half("Region", existing.region or "" if existing else "")
            cluster_id.bind_visibility_from(
                cluster_type, "value", backward=lambda v: v == "provisioned"
            )
            workgroup.bind_visibility_from(
                cluster_type, "value", backward=lambda v: v == "serverless"
            )

            _subheading(
                "AWS credentials", "Optional — falls back to the default AWS credential chain."
            )
            profile = _text(
                "AWS profile",
                existing.aws_profile or "" if existing else "",
                hint="Takes priority over the access keys below.",
            )
            with ui.row().classes("w-full gap-3"):
                access_key = _secret_half(
                    "Access key ID",
                    configured=bool(existing and load_secret(existing.access_key_env)),
                )
                secret_key = _secret_half(
                    "Secret access key",
                    configured=bool(existing and load_secret(existing.secret_key_env)),
                )

        with ui.tab_panel(tab_ssh):
            ui.label(
                "Only used when Connection mode (General tab) is Default — ignored "
                "otherwise, and requires a Provisioned cluster (Serverless workgroups "
                "don't support it)."
            ).classes("text-xs -mt-2").style(f"color:{chrome.INK_MUTED}")
            with ui.row().classes("w-full gap-3"):
                bastion_host = _half(
                    "Bastion host", existing.bastion_host or "" if existing else ""
                )
                bastion_port = _half(
                    "Bastion port",
                    str(existing.bastion_port) if existing and existing.bastion_port != 22 else "",
                )
            bastion_user = _text(
                "Bastion SSH user", existing.bastion_user or "" if existing else ""
            )
            bastion_key_path = _text(
                "Bastion private key path",
                existing.bastion_private_key_path or "" if existing else "",
            )
            bastion_passphrase_env_name = (
                existing.bastion_private_key_passphrase_env if existing else None
            )
            bastion_passphrase = _secret(
                "Bastion key passphrase",
                hint="Only needed if the private key itself is passphrase-protected.",
                configured=bool(
                    bastion_passphrase_env_name and load_secret(bastion_passphrase_env_name)
                ),
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
        is_direct = connection_mode.value == "direct"
        try:
            cfg = RedshiftConfig(
                name=name.value or None,
                cluster_identifier=(
                    cluster_id.value or None if cluster_type.value == "provisioned" else None
                ),
                workgroup_name=(
                    workgroup.value or None if cluster_type.value == "serverless" else None
                ),
                database=database.value or "dev",
                db_user=db_user.value or None,
                secret_arn=secret_arn.value or None if not is_direct else None,
                region=region.value or "us-east-1",
                aws_profile=profile.value or None,
                db_host=db_host.value or None if is_direct else None,
                db_port=(int(db_port.value) if db_port.value else None) if is_direct else None,
                bastion_host=bastion_host.value or None if is_direct else None,
                bastion_port=(int(bastion_port.value) if bastion_port.value else 22),
                bastion_user=bastion_user.value or None if is_direct else None,
                bastion_private_key_path=bastion_key_path.value or None if is_direct else None,
                db_password_env=db_password_env if is_direct else None,
                bastion_private_key_passphrase_env=bastion_passphrase_env if is_direct else None,
            )
        except (ValidationError, ValueError) as exc:
            ui.notify(str(exc), type="negative")
            return None
        secrets = {}
        if access_key.value:
            secrets[cfg.access_key_env] = access_key.value
        if secret_key.value:
            secrets[cfg.secret_key_env] = secret_key.value
        if is_direct and db_password.value and db_password_env:
            secrets[db_password_env] = db_password.value
        if is_direct and bastion_passphrase.value and bastion_passphrase_env:
            secrets[bastion_passphrase_env] = bastion_passphrase.value
        return cfg, secrets

    ui.separator()
    with ui.row().classes("w-full items-center gap-3"):
        test_button = ui.button("Test connection", icon="bolt").props("flat no-caps")
        test_status = ui.label("").classes("text-xs").style(f"color:{chrome.INK_MUTED}")

    async def _test_connection() -> None:
        result = collect()
        if result is None:
            return  # collect() already ui.notify'd the validation error
        cfg, typed_secrets = result
        assert isinstance(cfg, RedshiftConfig)  # collect() always builds one
        test_button.props("loading")
        test_status.set_text("Testing…")
        # A freshly-typed value (not yet saved) wins; otherwise fall back to whatever's
        # already in the keychain — same resolution a real sync uses (ingest_runner's
        # own _secrets_env), so testing an unchanged connection doesn't require
        # retyping a password just to prove it still works.
        env_names = [cfg.access_key_env, cfg.secret_key_env]
        if cfg.db_password_env:
            env_names.append(cfg.db_password_env)
        if cfg.bastion_private_key_passphrase_env:
            env_names.append(cfg.bastion_private_key_passphrase_env)
        test_env = {
            name: value
            for name in env_names
            if (value := typed_secrets.get(name) or load_secret(name))
        }
        prior = {k: os.environ.get(k) for k in test_env}
        os.environ.update(test_env)
        try:
            # ponytail: process-global os.environ mutation — two browser tabs testing
            # different Redshift connections at once could stomp each other's
            # secrets mid-test. Fine for a single-user local dashboard; move to a
            # per-call credential-injection path if this ever needs concurrent use.
            message = await asyncio.wait_for(
                run.io_bound(lambda: RedshiftConnector(cfg).test_connection()),
                timeout=_TEST_CONNECTION_TIMEOUT_SECS,
            )
            if message is not None:  # None only on cancel/app-shutdown (run.io_bound)
                test_status.set_text(message)
                ui.notify(message, type="positive")
        except TimeoutError:
            # ponytail: run.io_bound's underlying thread keeps running after we give up
            # waiting on it (Python threads can't be cancelled) — it'll finish or fail on
            # its own with nothing observing the result. Acceptable for a manual test
            # click on a single-user local dashboard.
            test_status.set_text("Test timed out")
            ui.notify(
                f"Timed out after {_TEST_CONNECTION_TIMEOUT_SECS}s — check the host/port "
                "and that this network can reach it.",
                type="negative",
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure, don't crash the handler
            test_status.set_text("Test failed")
            ui.notify(str(exc), type="negative")
        finally:
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            test_button.props(remove="loading")

    test_button.on_click(_test_connection)
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
    chrome.section_caption("Connect a billing source and sync it on a schedule you control.")

    # Sync is the page's primary action, so its controls sit at the top in one
    # toolbar — not buried in their own section below the connections list.
    # The date-range control is the same popover used on every other page
    # (chrome.date_range_control), not a bespoke one-off, so it looks and
    # behaves like the rest of the app. Bounds are a fixed 5-year floor (this
    # picks a source *pull window*, not a filter over data already on disk —
    # there's no real dataset to bound it against), defaulting to the trailing
    # 3 months: the CLI's own 35-day default is shorter than a dashboard user
    # opening this for the first time would expect.
    today = date.today()
    range_state: chrome.DateState = {
        "start": chrome.months_back(today, 3),
        "end": today,
        "bounds_min": chrome.months_back(today, 60),
        "bounds_max": today,
    }

    with chrome.panel():
        with ui.row().classes("w-full items-center gap-4"):
            chrome.date_range_control(range_state, lambda: None)
            with ui.row().classes("items-center gap-1"):
                full_refresh_checkbox = ui.checkbox("Full refresh")
                ui.icon("info", size="16px").style(f"color:{chrome.INK_MUTED}").tooltip(
                    "Wipes and re-pulls each connector's entire history instead of just "
                    "the selected range. Use after changing a connector's settings."
                )
            ui.space()
            sync_button = ui.button("Sync now", icon="sync").props("no-caps color=primary")

    # A sync that's already running — started from this tab, another tab, or before
    # this page load happened at all — keeps going in the background regardless of
    # what this render does (see ingest_runner's module docstring); this banner is
    # purely so a tab that lands here mid-sync doesn't look like nothing is happening.
    # ``ui.refreshable`` rather than a one-shot check because closing the dialog below
    # (or the sync finishing while this tab watches) needs to bring it back down.
    @ui.refreshable
    def sync_status_row() -> None:
        running = ingest_runner.current_run()
        if running is None:
            return
        with chrome.panel():
            with ui.row().classes("w-full items-center gap-3"):
                ui.spinner(size="1.2rem").style(f"color:{chrome.ACCENT}")
                ui.label(
                    f"Sync in progress — {running.connector or 'all connections'} "
                    f"(started {running.started_at.strftime('%H:%M UTC')})"
                ).classes("text-sm").style(f"color:{chrome.INK_PRIMARY}")
                ui.space()
                ui.button(
                    "View log",
                    icon="visibility",
                    on_click=lambda: _watch(running.total, running.connector),
                ).props("flat no-caps")

    sync_status_row()

    @ui.refreshable
    def connections_body() -> None:
        all_connections = load_all_connections(str(paths.connections_path()))
        if not all_connections:
            with chrome.panel():
                chrome.empty_state(
                    "cable",
                    "No data sources yet",
                    "Connect an AWS, Databricks, or Redshift billing source to start "
                    "seeing spend here.",
                    button_label="Add connection",
                    on_click=lambda: _open_dialog(None, None, all_connections),
                )
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
                with ui.row().classes("items-center gap-3 pl-6" if sub else "items-center gap-3"):
                    if not sub:
                        ui.icon(_TYPE_ICONS.get(ctype, "cloud"), size="1.25rem").style(
                            f"color:{chrome.ACCENT}"
                        )
                    with ui.column().classes("gap-0.5"):
                        label_classes = "text-sm font-medium" if sub else "text-base font-semibold"
                        ui.label(f"{_TYPE_LABELS.get(ctype, ctype)}: {cfg_name}").classes(
                            label_classes
                        ).style(f"color:{chrome.INK_PRIMARY}")
                        ui.label(_summary(cfg)).classes("text-xs").style(
                            f"color:{chrome.INK_MUTED}"
                        )
                with ui.row().classes("items-center gap-3"):
                    chrome.status_badge(cfg_enabled)
                    sync_row_button = (
                        ui.button(icon="sync").props("flat dense round").tooltip(f"Sync {cfg_name}")
                    )
                    sync_row_button.on_click(lambda cfg_name=cfg_name: _sync(cfg_name))
                    # Disabled during any sync, not just one this row started — this
                    # dashboard runs one sync at a time (see ingest_runner.start_sync).
                    if not cfg_enabled or ingest_runner.is_running():
                        sync_row_button.disable()
                    with ui.button(icon="more_vert").props("flat dense round"):
                        with ui.menu():
                            ui.menu_item(
                                "Edit",
                                on_click=lambda cfg=cfg, i=i: _open_dialog(i, cfg, all_connections),
                            )
                            ui.menu_item(
                                "Duplicate",
                                on_click=lambda cfg=cfg: _open_dialog(
                                    None,
                                    cfg.model_copy(update={"name": f"{cfg_name} (copy)"}),
                                    all_connections,
                                ),
                            )
                            ui.separator()
                            ui.menu_item(
                                "Delete",
                                on_click=lambda i=i, cfg_name=cfg_name: _confirm_delete(
                                    i, cfg_name, all_connections
                                ),
                            ).style(f"color:{chrome.WASTE}")

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

    def _confirm_delete(index: int, name: str, all_connections: list[BaseModel]) -> None:
        with ui.dialog() as dialog, ui.card().classes("gap-3 p-5"):
            ui.label(f'Delete "{name}"?').classes("text-base font-semibold").style(
                f"color:{chrome.INK_PRIMARY}"
            )
            ui.label(
                "This removes it from connections.yml — its BRONZE data and sync "
                "history are kept until you re-sync or clean the lake manually."
            ).classes("text-xs").style(f"color:{chrome.INK_MUTED}")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

                def _confirmed() -> None:
                    dialog.close()
                    _delete(index, all_connections)

                ui.button("Delete", on_click=_confirmed).props("flat no-caps").style(
                    f"color:{chrome.WASTE}"
                )
        dialog.open()

    def _open_dialog(
        existing_index: int | None, prefill: BaseModel | None, all_connections: list[BaseModel]
    ) -> None:
        type_key: str = getattr(prefill, "type") if prefill else "aws_focus"  # noqa: B009
        with (
            ui.dialog() as dialog,
            ui.card()
            .classes("gap-3 p-5")
            .style("width:640px; max-width:92vw; max-height:85vh; overflow-y:auto;"),
        ):
            ui.label("Edit connection" if existing_index is not None else "Add connection").classes(
                "text-base font-semibold"
            ).style(f"color:{chrome.INK_PRIMARY}")
            ui.label(
                "Credentials are stored securely in your OS keychain, never on disk in plain text."
            ).classes("text-xs").style(f"color:{chrome.INK_MUTED}")

            type_select = ui.select(_TYPE_LABELS, value=type_key, label="Type").classes("w-full")
            if existing_index is not None:
                type_select.disable()
            enabled_checkbox = _checkbox(
                "Enabled",
                getattr(prefill, "enabled") if prefill else True,  # noqa: B009
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

    with chrome.panel():
        with ui.row().classes("w-full items-center justify-between"):
            chrome.panel_title("Recent sync history")
            connector_filter = ui.select(["All"], value="All")

        # Timestamps shown in UTC (read_runs()/read_run_groups() are already UTC —
        # see runlog.py's schema), not the browser's local timezone. A prior
        # version tried to detect that client-side and reflow the table, three
        # different ways (a ui.timer(0.1, ..., once=True): fired after the page's
        # slot was sometimes already torn down; a background_tasks.create() task:
        # has no slot context at all, so touching any UI from inside one raises
        # immediately; then awaiting inline in render(): blocks everything after
        # it — including wiring up the Sync button's own click handler — behind
        # a client/JS round trip that isn't guaranteed to resolve promptly).
        # UTC everywhere sidesteps all of that for a cosmetic nicety.
        #
        # One row per whole sync (grouped by the shared run_id every connector in
        # that run_ingest() call stamps — see runner.py::run_connector), not one
        # row per connector: a 5-connector sync used to show as 5 disconnected
        # table rows with no sense of "this was one sync". Each expands to its own
        # per-connector breakdown, and links to the saved transcript
        # stream_sync() wrote line-by-line as it tailed that sync (survives
        # closing the live dialog that started it — see ingest_runner.py).
        @ui.refreshable
        def history_body() -> None:
            groups = read_run_groups()
            if groups.empty:
                chrome.empty_state("history", "No syncs yet", "Run a sync to see its history here.")
                return
            runs_detail = read_runs(limit=1000)
            connector_filter.set_options(["All", *sorted(runs_detail["connector"].unique())])
            if connector_filter.value != "All":
                matching = set(
                    runs_detail.loc[runs_detail["connector"] == connector_filter.value, "run_id"]
                )
                groups = groups[groups["run_id"].isin(matching)]
                if groups.empty:
                    chrome.empty_state(
                        "history", "No syncs yet", "Run a sync to see its history here."
                    )
                    return
            for _, run in groups.iterrows():
                run_id = run["run_id"]
                started = pd.Timestamp(run["started_at"]).strftime("%Y-%m-%d %H:%M %Z")
                failed = run["status"] == "failed"
                connectors_df = runs_detail[runs_detail["run_id"] == run_id].sort_values(
                    "connector"
                )
                log_path = paths.sync_log_path(run_id)
                with ui.row().classes("w-full items-center gap-2"):
                    expansion = (
                        ui.expansion(
                            f"{'✗' if failed else '✓'}  {started} · {int(run['rows']):,} rows · "
                            f"{int(run['connectors'])} connector(s)"
                        )
                        .classes("flex-1")
                        .style(f"color:{chrome.WASTE if failed else chrome.INK_PRIMARY}")
                    )
                    if log_path.exists():
                        ui.button(
                            icon="description",
                            on_click=lambda p=log_path, s=started: _open_saved_log(p, s),
                        ).props("flat dense round").tooltip("View saved log")
                with expansion, ui.column().classes("w-full gap-1 pl-2"):
                    for _, row in connectors_df.iterrows():
                        row_failed = row["status"] == "failed"
                        detail = row.get("detail")  # None unless this connector failed
                        with ui.column().classes("w-full gap-0"):
                            with ui.row().classes("w-full items-center gap-3"):
                                ui.label(row["connector"]).classes("text-xs").style(
                                    f"color:{chrome.INK_PRIMARY}; width:160px"
                                )
                                ui.label("failed" if row_failed else "ok").classes(
                                    "text-xs"
                                ).style(
                                    f"color:{chrome.WASTE if row_failed else chrome.OPPORTUNITY}; "
                                    "width:60px"
                                )
                                ui.label(f"{int(row['rows']):,} rows").classes("text-xs").style(
                                    f"color:{chrome.INK_MUTED}; width:110px"
                                )
                            if detail:
                                ui.label(str(detail)).classes("text-xs pl-4").style(
                                    f"color:{chrome.WASTE}; white-space:normal; "
                                    "word-break:break-word;"
                                )

        def _open_saved_log(path: Path, label: str) -> None:
            try:
                text = path.read_text()
            except OSError as exc:
                ui.notify(f"Couldn't read log: {exc}", type="negative")
                return
            with ui.dialog() as dialog, ui.card().style("width:700px; max-width:95vw;"):
                ui.label(f"Sync log · {label}").classes("text-sm font-semibold").style(
                    f"color:{chrome.INK_PRIMARY}"
                )
                log_widget = (
                    ui.log(max_lines=5000).classes("w-full").style("height:50vh; font-size:12px;")
                )
                for line in text.splitlines():
                    log_widget.push(line)
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button(
                        "Download",
                        icon="download",
                        on_click=lambda: ui.download(text.encode(), f"{path.stem}.log"),
                    ).props("flat no-caps")
                    ui.button("Close", on_click=dialog.close).props("flat no-caps")
            dialog.open()

        history_body()

    connector_filter.on_value_change(history_body.refresh)

    async def _watch(total: int, connector: str | None) -> None:
        """Open the live-tail dialog and follow the current sync to completion —
        whether *this* call is what just started it, or it's already running
        from an earlier click and this is the "Sync in progress" banner's "View
        log" button reattaching to it. Either way the dialog opens immediately
        and tails the subprocess live instead of showing a bare spinner and
        dumping everything at the end — a sync can run for minutes, and "is it
        doing anything?" was the whole complaint. A "N of M cost pulls done"
        counter (parsed from the same progress lines the tail already shows —
        see _CONNECTOR_DONE_RE's own comment for why it's phrased around the
        cost pull specifically, not the whole sync) and a "Download log" button
        (the accumulated text, client-side — no server route or on-disk log
        file needed) ride along for free.

        Cancelling this coroutine (dialog closed, tab navigated away) only
        detaches this one viewer — the sync itself runs in ingest_runner's own
        module-level background task and keeps going either way; see its
        module docstring for why that split exists.
        """
        lines: list[str] = list(ingest_runner.recent_lines())
        done = sum(1 for line in lines if _CONNECTOR_DONE_RE.match(line))

        with ui.dialog() as log_dialog, ui.card().style("width:700px; max-width:95vw;"):
            ui.label(f"Syncing {connector or 'all connections'}...").classes(
                "text-sm font-semibold"
            ).style(f"color:{chrome.INK_PRIMARY}")
            progress_label = (
                ui.label(f"{done} / {total} cost pulls done")
                .classes("text-xs")
                .style(f"color:{chrome.INK_SECONDARY}")
            )
            log_widget = (
                ui.log(max_lines=2000).classes("w-full").style("height:50vh; font-size:12px;")
            )
            for line in lines:
                log_widget.push(line)
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

        client_gone = False

        def _on_line(line: str) -> None:
            nonlocal done, client_gone
            lines.append(line)
            if client_gone:
                # The browser tab/dialog is gone (navigated away, closed, reloaded)
                # but the subprocess keeps streaming — every element under it was
                # already torn down, so further pushes would just re-raise this
                # same RuntimeError once per remaining line.
                return
            try:
                log_widget.push(line)
                if _CONNECTOR_DONE_RE.match(line):
                    done += 1
                    progress_label.set_text(f"{done} / {total} cost pulls done")
            except RuntimeError:
                client_gone = True

        unsubscribe = ingest_runner.subscribe(_on_line)
        try:
            result = await ingest_runner.wait_for_current()
        finally:
            unsubscribe()

        if client_gone or result is None:
            return

        # Unlike the interim per-line updates above, this fires only once the
        # subprocess has actually exited — so unlike those, "done" here really does
        # mean the whole sync (cost + efficiency + driver-health + GOLD rebuild).
        returncode, _run_id = result
        progress_label.set_text(f"Sync finished — exit code {returncode}")
        ui.notify(
            "Sync completed" if returncode == 0 else "Sync failed — see output above",
            type="positive" if returncode == 0 else "negative",
        )
        history_body.refresh()
        connections_body.refresh()
        sync_status_row.refresh()

    async def _sync(connector: str | None = None) -> None:
        """Starts a sync — "Sync now" (``connector=None``, every enabled
        connector) or a row's own Sync button (``connector=<its effective
        name>``) — then watches it via :func:`_watch`.

        Starting and watching are deliberately two separate steps:
        :func:`ingest_runner.start_sync`'s own background task is what keeps the
        sync alive independent of this coroutine (see its module docstring);
        :func:`_watch` merely observes it, and stops observing — not stops it —
        if this tab goes away.
        """
        if ingest_runner.is_running():
            ui.notify("A sync is already running", type="warning")
            return
        start, end = range_state["start"], range_state["end"]
        total = 1 if connector is not None else len(load_connections(str(paths.connections_path())))
        try:
            await ingest_runner.start_sync(
                paths.connections_path(),
                full_refresh=full_refresh_checkbox.value,
                connector=connector,
                start=start,
                end=end,
            )
        except Exception as exc:  # noqa: BLE001 - surface a launch failure, not a crash
            ui.notify(f"Sync failed to start: {exc}", type="negative")
            return
        sync_status_row.refresh()
        connections_body.refresh()
        await _watch(total, connector)

    sync_button.on_click(lambda: _sync())
