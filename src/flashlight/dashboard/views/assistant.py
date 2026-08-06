"""BYOK assistant — ask Flashlight's own GOLD data questions with your own LLM key.

Settings (provider/model/base URL/API key) live behind a gear-icon dialog that
auto-opens only when nothing is configured yet.

Provider/model/base URL are persisted to ``FLASHLIGHT_HOME/config/assistant.yml``
(:mod:`flashlight.dashboard.assistant_config`), beside ``connections.yml`` and
``policies.yml``. None of the three is a secret, so a plain file is fine. They used to
be bound to NiceGUI's ``app.storage.general``, which lands wherever
``NICEGUI_STORAGE_PATH`` points — a tmpfs on a read-only deployment — so a restart
forgot the model and re-opened this dialog.

The API key is different — see :mod:`flashlight.dashboard.assistant_credentials`
for why it goes through the OS keychain (with an env-var fallback) instead of
sitting in that same file: an app-managed "encrypted at rest" key isn't real
protection (the decryption key would have to live somewhere the app itself
can read, and so can anyone with the same file access), whereas the OS
keychain's protection comes from your login session, not from Flashlight. If
the keychain write fails (e.g. a headless box with no secret-service daemon),
the key still survives within the current browser tab via ``app.storage.tab``
— cleared on tab close — and the user is told once via ``ui.notify`` how to
make it durable (the env var).

That storage isn't ready during the very first synchronous slice of a page
render — ``await ui.context.client.connected()`` up front is the documented,
verified-safe way to wait for it (confirmed directly against this NiceGUI
version rather than assumed).

The conversation itself is not persisted. Assistant replies are rendered as
sanitized markdown (via the ``markdown2`` package NiceGUI already depends on)
so lists/bold/code in an answer actually render, not just show up as literal
asterisks.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import plotly.express as px
from nicegui import app, ui
from pydantic_ai.messages import ModelMessage

from flashlight.dashboard import assistant_config, assistant_credentials, chrome
from flashlight.dashboard.answer_caption import is_money_column, is_temporal_column
from flashlight.dashboard.assistant_engine import ChartSpec, ToolStep, run_turn


def _informative_dims(df: pd.DataFrame, varying_cols: list[str]) -> list[str]:
    """Drop dimensions that add no distinguishing information — the ones already
    determined by a finer column present in the same rows.

    A constant column is the obvious case (already filtered out by the caller),
    but ``service_category`` is the same thing one step up: every ``service_name``
    has exactly one category, so it splits nothing. It still *varies*, so it
    counted as a third dimension and pushed the real screenshot case
    (category + service + month) past the two-dimension limit into a table.
    Keeping only informative columns leaves service x month — a stacked bar.
    """
    def preference(col: str) -> tuple[bool, int, int]:
        """Which of two interchangeable columns to keep, highest first.

        Cardinality is the main signal (the finer column determines the coarser,
        not vice versa), but two columns are often *mutually* determining — a
        1:1 pair like service_category/service_name when each category happens to
        have one service in the result. Dropping the wrong one of those silently
        loses the axis that matters, so break the tie deliberately: keep a
        temporal column (a trend must stay drawable on time), then the more
        specific label, which the catalog puts further right ("JOBS" over
        "Analytics").
        """
        is_temporal = is_temporal_column(col)
        return is_temporal, df[col].nunique(dropna=False), varying_cols.index(col)

    kept: list[str] = []
    # Most-preferred first, so a droppable column is tested against the one that
    # should survive it, not the other way round.
    for col in sorted(varying_cols, key=preference, reverse=True):
        determined_by_a_kept_col = any(
            df.groupby(k, dropna=False)[col].nunique(dropna=False).max() <= 1 for k in kept
        )
        if determined_by_a_kept_col:
            continue
        kept.append(col)
    return [c for c in varying_cols if c in kept]  # back to the view's column order


def _infer_spec(df: pd.DataFrame, varying_cols: list[str]) -> tuple[str, str | None, str] | None:
    """Infer ``(x, series, kind)`` from the row shape when the model declared no
    ChartSpec — the deterministic floor under the declaration.

    It has to be a real floor, not a formality: a live gpt-oss-20b declared a
    chart for "show me the monthly trend" but *not* for "visualize databricks
    spend year to date by service", the very question that needs one. A weak
    model complies probabilistically, so the no-declaration path still has to
    produce something readable.

    query_metric returns *every* dimension of a view (there's no way to narrow
    them the way `measures` narrows measures), so results routinely carry two
    real dimensions — and a second dimension is a *series*, not extra text to
    staple onto the axis label. Treating it as the latter is what drew 39 bars
    titled "Networking · NETWORKING · 2026-07-01".

    The hard requirement either way is **one row per drawn point**: repeats
    would stack several segments where the reader sees one value. Anything with
    more than two varying dimensions has no honest 2-D reading, so it stays a
    table.
    """
    varying_cols = _informative_dims(df, varying_cols)
    unique = [c for c in varying_cols if df[c].nunique(dropna=False) == len(df)]
    if unique:
        # Prefer a temporal column when one qualifies: "spend by month" should be
        # a time series even if some other column happens to be unique too.
        temporal = [c for c in unique if is_temporal_column(c)]
        if temporal:
            return temporal[0], None, "bar"
        # Otherwise the most granular label. Several columns are often *equally*
        # unique (service_category and service_name both are, once one row per
        # service); the catalog lists a view's dimensions coarse-to-fine, so the
        # rightmost one is the more specific label — "JOBS" says more than
        # "Analytics".
        chosen = max(unique, key=lambda c: (df[c].nunique(dropna=False), varying_cols.index(c)))
        return chosen, None, "bar"
    if len(varying_cols) != 2 or len(df.drop_duplicates(subset=varying_cols)) != len(df):
        return None
    # Two dimensions, one row per pair: a stacked bar. The wider one goes on x so
    # the narrower one becomes the (legible, palette-sized) stack — 13 services
    # split by 3 months, not 3 months split by 13 services.
    by_width = sorted(varying_cols, key=lambda c: df[c].nunique(dropna=False), reverse=True)
    return by_width[0], by_width[1], "stacked_bar"


def _resolve_chart(
    df: pd.DataFrame, chart: ChartSpec | None, numeric_cols: list[str]
) -> tuple[str, str | None, str] | None:
    """Validate a model-declared ChartSpec against the columns that actually came
    back — ``(x, series, kind)`` if it holds up, else None to fall back to
    shape inference.

    The model names columns from the catalog in its prompt, not from the result,
    so it can name one this particular query didn't return (a narrowed
    `measures`, a different view than it meant). Never trust it blind: an
    unknown column would raise inside Plotly, and a non-unique (x, series) pair
    would quietly draw two segments where the reader sees one total.
    """
    if chart is None or len(numeric_cols) != 1:
        return None
    if chart.x not in df.columns:
        return None
    series = chart.series if chart.series and chart.series != chart.x else None
    if series is not None and series not in df.columns:
        return None
    keys = [chart.x] if series is None else [chart.x, series]
    if len(df.drop_duplicates(subset=keys)) != len(df):
        return None
    return chart.x, series, chart.kind


def _render_rows(rows: list[dict[str, Any]], *, key: str, chart: ChartSpec | None = None) -> None:
    """Draw the chart the model asked for; fall back to inferring one from the
    row shape; fall back again to a plain table. Never a general auto-viz
    engine — a wrong guess here means a table instead of a chart, never a crash.
    """
    df = pd.DataFrame(rows)
    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    other_cols = [c for c in df.columns if c not in numeric_cols]
    # A column that never varies (e.g. provider_name — still present, constant,
    # on a per-provider-sliced view like aws.monthly_bill even once filtered/
    # narrowed to one provider) carries no charting information; only a column
    # that actually varies is a candidate for the chart's axis.
    varying_other_cols = [c for c in other_cols if df[c].nunique(dropna=False) > 1]
    chartable = len(numeric_cols) == 1 and bool(varying_other_cols) and 2 <= len(df) <= 200
    spec = _resolve_chart(df, chart, numeric_cols) if chartable else None
    if spec is None and chartable:
        # Nothing usable declared — an MCP agent never declares, and a weak model
        # often forgets to, so fall back to reading the shape.
        spec = _infer_spec(df, varying_other_cols)
    if spec is not None:
        _plot(df, spec, numeric_cols[0])
    else:
        money_cols = [c for c in numeric_cols if is_money_column(c)]
        num_cols = [c for c in numeric_cols if c not in money_cols]
        chrome.flat_table(df, key=key, money_cols=money_cols, num_cols=num_cols)


def _plot(df: pd.DataFrame, spec: tuple[str, str | None, str], y_col: str) -> None:
    x_col, series, kind = spec
    is_money = is_money_column(y_col)
    value_format = "$%{y:,.0f}" if is_money else "%{y:,.0f}"
    temporal_x = is_temporal_column(x_col)
    if series is not None:
        df = chrome.cap_series(df, series, y_col)
    # A category breakdown reads as a ranking, so order it by size; a time axis
    # must keep its own order or the trend is destroyed. With a series, rank by
    # each x value's total rather than by an arbitrary segment.
    if not temporal_x:
        totals = df.groupby(x_col)[y_col].sum().sort_values(ascending=False)
        rank = {k: i for i, k in enumerate(totals.index)}
        df = df.sort_values(x_col, key=lambda col: col.map(rank))
    else:
        df = df.sort_values(x_col)
    labels = {x_col: "", y_col: ""}
    if kind == "line":
        fig = px.line(
            df, x=x_col, y=y_col, color=series, labels=labels, markers=True,
            color_discrete_sequence=list(chrome.CATEGORICAL_SLOTS),
        )
    else:
        # Any bar chart with a series is stacked, whichever bar kind was asked
        # for: grouping put 8 months x 13 services side by side as 104 thin bars
        # (a live model declared kind="bar" with series="charge_month" for a
        # year-to-date breakdown), which is the same unreadability the composite
        # axis had. Spend is additive, so a stack's total is the number the
        # question was actually about — and ChartSpec has no "grouped" kind to
        # override this with, deliberately.
        fig = px.bar(
            df, x=x_col, y=y_col, color=series, labels=labels,
            color_discrete_sequence=list(chrome.CATEGORICAL_SLOTS),
            barmode="stack",
        )
    if series is None:
        # Per-value labels only make sense on a single series; on a stack they
        # collide with the segments above them. Colour has to be set with the
        # property that trace type actually has — passing `line_color` to a Bar
        # is rejected by Plotly even as None.
        styling: dict[str, Any] = {
            "text": df[y_col],
            "texttemplate": value_format.replace("%{y", "%{text"),
            "cliponaxis": False,
        }
        if kind == "line":
            styling |= {"line_color": chrome.ACCENT, "textposition": "top center"}
        else:
            styling |= {"marker_color": chrome.ACCENT, "textposition": "outside"}
        fig.update_traces(**styling)
    fig.update_traces(hovertemplate=f"%{{x}}<br>{value_format}<extra></extra>")
    chrome.plot(
        chrome.style_fig(
            fig,
            category_x=kind != "line" or not temporal_x,
            currency_axis="y" if is_money else None,
            title=y_col.replace("_", " ").title(),
            has_legend=series is not None,
        )
    )


def _render_tool_step(step: ToolStep, *, key: str) -> None:
    # The chart/table *is* the answer, so it renders inline, always visible —
    # only the raw call (SQL/args), a debugging detail, sits behind the fold.
    # run_sql is the exception: it's the model's own freeform SQL, not a
    # tested view, so a wrong GROUP BY/join/window function can produce a
    # plausible-looking but silently wrong chart — open by default so the
    # actual query is immediately auditable, not one extra click away.
    label = f"Queried {step.name}" if step.rows is not None else f"Called {step.name}"
    expansion = ui.expansion(label, icon="terminal", value=step.name == "run_sql").classes("w-full")
    expansion.style(f"color:{chrome.INK_SECONDARY}")
    with expansion:
        if step.name == "run_sql" and "sql" in step.arguments:
            ui.code(str(step.arguments["sql"]), language="sql").classes("w-full")
        elif step.arguments:
            ui.code(json.dumps(step.arguments, indent=2), language="json").classes("w-full")
    if step.error:
        ui.label(f"Error: {step.error}").classes("text-xs").style(f"color:{chrome.WASTE}")
    elif step.rows:
        _render_rows(step.rows, key=key, chart=step.chart)
    elif step.rows == []:
        ui.label("No rows returned.").classes("text-xs").style(f"color:{chrome.INK_MUTED}")


_PRESETS: dict[str, dict[str, str]] = {
    "OpenAI": {"provider": "openai", "model": "gpt-4o", "base_url": ""},
    "Anthropic (Claude)": {"provider": "anthropic", "model": "claude-sonnet-4-5", "base_url": ""},
    "Google (Gemini)": {"provider": "google", "model": "gemini-2.0-flash", "base_url": ""},
    # "openai_compatible" covers anything speaking the OpenAI chat-completions
    # wire format that isn't one of the three native providers above — Ollama's
    # own OpenAI-compatible endpoint lives under /v1.
    "Ollama (local)": {
        "provider": "openai_compatible",
        "model": "llama3",
        "base_url": "http://localhost:11434/v1",
    },
    # Databricks' Foundation Model API is OpenAI-compatible too — base_url is
    # the workspace's serving-endpoints URL, e.g.
    # https://<workspace-host>/serving-endpoints.
    "Databricks": {
        "provider": "openai_compatible",
        "model": "databricks-gpt-oss-20b",
        "base_url": "",
    },
    "Custom / self-hosted": {"provider": "openai_compatible", "model": "", "base_url": ""},
}
_DEFAULT_PROVIDER = "OpenAI"

# FLASHLIGHT_ASSISTANT_* name the internal provider id, not a dialog label — the env
# var for the settings field, for the readonly notice below.
_ENV_VARS = {
    "provider": "FLASHLIGHT_ASSISTANT_PROVIDER",
    "model": "FLASHLIGHT_ASSISTANT_MODEL",
    "base_url": "FLASHLIGHT_ASSISTANT_BASE_URL",
}


def _preset_label(cfg: assistant_config.AssistantConfig) -> str:
    """Which dropdown row to show for a stored config.

    ``cfg.preset`` is the label the user actually picked and is purely cosmetic, so it
    only wins while it still agrees with the load-bearing ``provider`` id (an env var
    can pin a different one). Otherwise fall back to the first preset carrying that id
    — which is also the only thing available when the config came from env vars alone,
    since those name the id and know nothing about labels.
    """
    if cfg.preset in _PRESETS and _PRESETS[cfg.preset]["provider"] == cfg.provider:
        return cfg.preset
    for label, preset in _PRESETS.items():
        if preset["provider"] == cfg.provider:
            return label
    return _DEFAULT_PROVIDER

# (headline, detail) — the two are concatenated into the question actually sent,
# so the detail carries real specificity into the prompt instead of being
# decorative subtitle text.
_SUGGESTIONS = (
    ("Break down last month's spend", "by service, across every connected provider"),
    ("Find recoverable spend", "idle and underutilized resources right now"),
    ("Compare month over month", "which service grew the most, and by how much"),
)

_DISCLAIMER = (
    "Responses are generated from your published FOCUS spend data and can "
    "contain errors — verify figures before acting on them."
)


async def render() -> None:
    await ui.context.client.connected()  # general/tab storage are only ready after this resolves
    if ui.context.client.is_deleted:
        # connected() also returns for a client NiceGUI pruned while it was still
        # waiting for a socket that never showed up (e.g. a prefetch/crawler GET,
        # or a tab that never opened its websocket) — not a real page load, so
        # there's no client-bound storage (app.storage.tab) left to render into.
        return

    def _load_key_for(provider: str) -> str:
        """Same-tab edits (if a keychain write failed) win over the persisted value."""
        same_tab = app.storage.tab.get(f"assistant_api_key:{provider}", "")
        return same_tab or assistant_credentials.load_api_key(provider) or ""

    def _apply_preset(label: str | None) -> None:
        preset = _PRESETS.get(label or "")
        if preset is not None:
            model_input.value = preset["model"]
            base_url_input.value = preset["base_url"]

    def _on_provider_change(label: str | None) -> None:
        _apply_preset(label)
        api_key_input.value = _load_key_for(label or "")

    cfg = assistant_config.load()
    pinned = assistant_config.env_overrides()

    with ui.dialog() as settings_dialog, ui.card().classes("gap-3 p-5").style("width:420px"):
        ui.label("Assistant settings").classes("text-base font-semibold").style(
            f"color:{chrome.INK_PRIMARY}"
        )
        ui.label(
            "Pick a provider to prefill the model, or choose Custom for a "
            "self-hosted / OpenAI-compatible endpoint. Provider/model/base URL are saved "
            "to config/assistant.yml; the key is stored in your OS keychain."
        ).classes("text-xs").style(f"color:{chrome.INK_MUTED}")
        if pinned:
            # Shown, and the fields locked, rather than letting an edit look like it
            # took: the env value wins on every load, so a "saved" change to a pinned
            # field would read as the setting not sticking.
            ui.label(
                "Set by environment, so read-only here: "
                + ", ".join(_ENV_VARS[f] for f in sorted(pinned))
            ).classes("text-xs").style(f"color:{chrome.SEMANTIC['unattributed']}")

        # provider_select's on_change is wired up AFTER model_input/base_url_input/
        # api_key_input exist (below), not passed as a constructor kwarg here: an
        # initial value sync fires on_change synchronously, during this very statement
        # — while model_input et al. are still unassigned in this enclosing scope,
        # which crashed with "cannot access free variable 'model_input'" for anyone
        # with a previously-saved provider.
        provider_select = (
            ui.select(
                list(_PRESETS),
                label="Provider",
                value=_preset_label(cfg),
            )
            .classes("w-full")
            .mark("assistant-provider")
        )
        model_input = (
            ui.input("Model", value=cfg.model or "").classes("w-full").mark("assistant-model")
        )
        base_url_input = (
            ui.input(
                "Base URL (Databricks workspace, or self-hosted/custom)",
                value=cfg.base_url or "",
            )
            .classes("w-full")
            .mark("assistant-base-url")
        )
        api_key_input = (
            ui.input("API key").props("type=password").classes("w-full").mark("assistant-api-key")
        )
        for field, widget in (
            ("provider", provider_select),
            ("model", model_input),
            ("base_url", base_url_input),
        ):
            if field in pinned:
                widget.props("readonly")
        provider_select.on_value_change(lambda e: _on_provider_change(e.value))
        if not model_input.value:  # nothing configured yet — prefill from the preset
            _apply_preset(provider_select.value)
        api_key_input.value = _load_key_for(provider_select.value)

        def _save_settings() -> None:
            label = provider_select.value or _DEFAULT_PROVIDER
            preset = _PRESETS.get(label, _PRESETS[_DEFAULT_PROVIDER])
            assistant_config.save(
                assistant_config.AssistantConfig(
                    provider=preset["provider"],
                    model=model_input.value or None,
                    base_url=base_url_input.value or None,
                    preset=label,
                )
            )
            key = api_key_input.value
            if key:
                # Keyed by the dialog label, not the internal provider id: three presets
                # share `openai_compatible` (Ollama, Databricks, Custom), so keying by id
                # would make one key serve three different endpoints.
                app.storage.tab[f"assistant_api_key:{label}"] = key
                if not assistant_credentials.save_api_key(label, key):
                    ui.notify(
                        "Couldn't reach your OS keychain — your key will only last this "
                        f"browser tab. Set {assistant_credentials.ENV_VAR} to persist it instead.",
                        type="warning",
                        timeout=8000,
                    )
            settings_dialog.close()

        with ui.row().classes("w-full justify-end"):
            ui.button("Done", on_click=_save_settings).props(
                "flat no-caps color=primary"
            ).mark("assistant-settings-done")

    # A compact top bar (model name + actions) instead of a page title/caption
    # block: this page is a conversational surface, so the vertical space goes to
    # the transcript, the way every chat app spends it.
    with ui.row().classes("w-full items-center justify-between px-5 py-2 shrink-0").style(
        f"border-bottom:1px solid {chrome.BORDER};"
    ):
        status_label = ui.label().classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        with ui.row().classes("items-center gap-1"):
            ui.button(icon="insights", on_click=lambda: ui.navigate.to("/usage")).props(
                "flat round dense"
            ).tooltip("Usage").mark("assistant-usage-button")
            ui.button(icon="settings", on_click=settings_dialog.open).props(
                "flat round dense"
            ).mark("assistant-settings-button")

    def _refresh_status() -> None:
        status_label.text = (
            f"Using {model_input.value}"
            if api_key_input.value and model_input.value
            else "No API key set — click the gear icon to configure"
        )

    model_input.on_value_change(lambda _: _refresh_status())
    api_key_input.on_value_change(lambda _: _refresh_status())
    _refresh_status()

    if not api_key_input.value:
        settings_dialog.open()

    messages: list[ModelMessage] = []
    session_id = ui.context.client.tab_id or ui.context.client.id
    turn_counter = 0

    # Two layout modes, one composer. Empty state: everything (heading,
    # composer, suggestions) sits vertically centered in the page. First
    # question: the hero is hidden, the transcript takes the height, and the
    # same composer element is *moved* to the bottom bar — moved rather than
    # duplicated so there's only ever one input to keep in sync (NiceGUI's
    # Element.move, verified present in this version).
    with ui.column().classes("w-full items-stretch gap-0").style("flex:1;min-height:0;"):
        hero = ui.column().classes("w-full items-center justify-center gap-0 px-4").style(
            "flex:1;min-height:0;"
        )
        with hero:
            with ui.row().classes("items-center gap-3 pb-6"):
                ui.icon("bolt", size="2rem").style(f"color:{chrome.ACCENT}")
                ui.label("What do you want to know about your spend?").classes(
                    "text-2xl font-medium"
                ).style(f"color:{chrome.INK_PRIMARY}")
            composer_slot_hero = ui.column().classes("w-full items-center gap-0")
            with ui.column().classes("w-full max-w-3xl gap-0 pt-6"):
                with ui.row().classes("items-center gap-1 pb-1"):
                    ui.icon("bolt", size="0.9rem").style(f"color:{chrome.INK_MUTED}")
                    ui.label("Suggested").classes("text-xs font-medium").style(
                        f"color:{chrome.INK_MUTED}"
                    )
                for i, (headline, detail) in enumerate(_SUGGESTIONS):
                    row = (
                        ui.column()
                        .classes("w-full gap-0 py-2 px-2 rounded-lg cursor-pointer")
                        .mark(f"assistant-suggestion-{i}")
                    )
                    with row:
                        ui.label(headline).classes("text-base").style(
                            f"color:{chrome.INK_PRIMARY}"
                        )
                        ui.label(detail).classes("text-xs").style(f"color:{chrome.INK_MUTED}")
                    row.on(
                        "click",
                        lambda h=headline, d=detail: _use_suggestion(f"{h} — {d}"),
                    )

        transcript = ui.scroll_area().classes("w-full").style("flex:1;min-height:0;")
        transcript.set_visibility(False)
        with transcript:
            message_area = ui.column().classes("w-full max-w-3xl mx-auto gap-3 px-4 py-6")
        scroll_area = transcript

        async def _use_suggestion(prompt: str) -> None:
            input_box.value = prompt
            await send()

        def _scroll_down() -> None:
            scroll_area.scroll_to(percent=1.0)

        async def send(*, answering_clarification: bool = False) -> None:
            nonlocal turn_counter
            question = input_box.value.strip()
            if not question:
                return
            if not model_input.value or not api_key_input.value:
                ui.notify("Set a model and API key first", type="warning")
                settings_dialog.open()
                return
            if hero.visible:
                # First question of the session: leave the centered empty state
                # for the transcript layout, taking the composer down with it.
                hero.set_visibility(False)
                transcript.set_visibility(True)
                bottom_bar.set_visibility(True)
                composer.move(composer_slot_bottom)
            input_box.value = ""
            with message_area:
                ui.chat_message(question, sent=True)
                with ui.row().classes("items-center gap-2") as thinking:
                    ui.spinner(size="1.2rem").style(f"color:{chrome.INK_MUTED}")
                    ui.label("Thinking...").classes("text-sm").style(f"color:{chrome.INK_MUTED}")
            _scroll_down()
            send_button.props("loading")
            input_box.props("disable")
            provider = _PRESETS.get(provider_select.value or "", _PRESETS[_DEFAULT_PROVIDER])[
                "provider"
            ]
            try:
                result = await run_turn(
                    messages,
                    question,
                    provider=provider,
                    api_key=api_key_input.value,
                    model=model_input.value,
                    base_url=base_url_input.value or None,
                    session_id=session_id,
                    answering_clarification=answering_clarification,
                )
            finally:
                thinking.delete()
                send_button.props(remove="loading")
                input_box.props(remove="disable")
            turn_counter += 1
            with message_area, ui.column().classes("w-full gap-2"):
                if result.reasoning:
                    # Collapsed by default — it's debugging detail, not the
                    # answer. Open it and it's the only record of what the model
                    # was trying to do on a turn that produced no answer at all.
                    with ui.expansion(
                        f"Model reasoning ({len(result.reasoning)} step"
                        f"{'s' if len(result.reasoning) > 1 else ''})",
                        icon="psychology",
                    ).classes("w-full").style(f"color:{chrome.INK_MUTED}").mark(
                        f"assistant-reasoning-{turn_counter}"
                    ):
                        for trace in result.reasoning:
                            ui.label(trace).classes("text-xs whitespace-pre-wrap").style(
                                f"color:{chrome.INK_MUTED}"
                            )
                for i, step in enumerate(result.steps):
                    _render_tool_step(step, key=f"assistant-step-{turn_counter}-{i}")
                ui.markdown(result.text, extras=["fenced-code-blocks", "tables"]).style(
                    f"color:{chrome.INK_PRIMARY}; line-height:1.6;"
                )
                if result.options:
                    # A vertical list, not a chip row: chips truncate to a short
                    # pill, which doesn't leave room for an option to actually
                    # describe itself (e.g. "All services across all providers
                    # (default)") — a full-width row wraps naturally instead.
                    with ui.list().props("bordered separator dense").classes("w-full").style(
                        f"border-radius:8px;border-color:{chrome.BORDER};"
                    ):
                        for i, option in enumerate(result.options):
                            ui.item(option, on_click=lambda o=option: _send_option(o)).style(
                                f"color:{chrome.INK_SECONDARY}"
                            ).mark(f"assistant-option-{turn_counter}-{i}")
            _scroll_down()

        async def _send_option(option: str) -> None:
            # Clicking an option we offered resolves the ambiguity by
            # construction — flagged so the engine can't answer it with yet
            # another clarifying question (confirmed live: a weak model asked
            # "which month?" straight after the user picked an option that
            # already said "for the previous month (default)").
            input_box.value = option
            await send(answering_clarification=True)

        # The bottom bar the composer moves into once a conversation starts —
        # hidden (and empty) while the centered empty state owns the composer.
        bottom_bar = ui.column().classes("w-full items-center shrink-0 px-4 pb-4 pt-1")
        bottom_bar.set_visibility(False)
        with bottom_bar:
            composer_slot_bottom = ui.column().classes("w-full items-center gap-0")
            ui.label(_DISCLAIMER).classes("text-xs pt-2 text-center").style(
                f"color:{chrome.INK_MUTED}"
            )

        # One soft rounded surface holding the field and send button, built
        # inside the empty state's slot and later moved to bottom_bar.
        with composer_slot_hero:
            composer = ui.row().classes("w-full max-w-3xl gap-2 items-center px-3 py-1").style(
                f"background:{chrome.SURFACE};border:1px solid {chrome.BORDER};"
                "border-radius:26px;"
            )
            with composer:
                input_box = (
                    ui.input(placeholder="Ask about your spend...")
                    .props("borderless dense")
                    .classes("flex-1 text-base")
                    .style("font-size:15px;")
                    .on("keydown.enter", send)
                    .mark("assistant-question")
                )
                send_button = (
                    ui.button(icon="arrow_upward", on_click=send)
                    .props("round dense color=primary")
                    .mark("assistant-send")
                )
