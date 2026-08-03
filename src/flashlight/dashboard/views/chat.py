"""BYOK chat — ask Flashlight's own GOLD data questions with your own LLM key.

Settings (provider/model/base URL/API key) live behind a gear-icon dialog that
auto-opens only when nothing is configured yet.

Provider/model/base URL are bound to NiceGUI's ``app.storage.general`` — a
single JSON file under ``FLASHLIGHT_HOME/meta/dashboard_storage/`` (wired up
in ``dashboard/launch.py``) — so they survive a process restart and a browser
cache clear, not just a page reload. Neither of those is a secret, so a plain
file is fine.

The API key is different — see :mod:`flashlight.dashboard.chat_credentials`
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

from flashlight.dashboard import chat_credentials, chrome
from flashlight.dashboard.chat_engine import ToolStep, run_turn

_MONEY_HINTS = ("cost", "amount", "spend", "price", "waste", "savings")


def _is_money_col(name: str) -> bool:
    return any(hint in name.lower() for hint in _MONEY_HINTS)


def _render_rows(rows: list[dict[str, Any]], *, key: str) -> None:
    """Chart when the shape is unambiguous (one dimension, one measure, a
    handful of rows); a plain table otherwise. Not a general auto-viz engine —
    a wrong guess here just means a table instead of a chart, never a crash.
    """
    df = pd.DataFrame(rows)
    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    other_cols = [c for c in df.columns if c not in numeric_cols]
    # A column that never varies (e.g. provider_name — still present, constant,
    # on a per-provider-sliced view like aws.monthly_bill even once filtered/
    # narrowed to one provider) carries no charting information; only a column
    # that actually varies counts as the chart's one real dimension.
    varying_other_cols = [c for c in other_cols if df[c].nunique(dropna=False) > 1]
    if len(numeric_cols) == 1 and len(varying_other_cols) == 1 and 2 <= len(df) <= 50:
        x_col, y_col = varying_other_cols[0], numeric_cols[0]
        is_money = _is_money_col(y_col)
        value_format = "$%{y:,.0f}" if is_money else "%{y:,.0f}"
        fig = px.bar(df, x=x_col, y=y_col, labels={x_col: "", y_col: ""})
        fig.update_traces(
            marker_color=chrome.ACCENT,
            text=df[y_col],
            texttemplate=value_format.replace("%{y", "%{text"),
            textposition="outside",
            cliponaxis=False,
            hovertemplate=f"%{{x}}<br>{value_format}<extra></extra>",
        )
        chrome.plot(
            chrome.style_fig(
                fig,
                category_x=True,
                currency_axis="y" if is_money else None,
                title=y_col.replace("_", " ").title(),
            )
        )
    else:
        money_cols = [c for c in numeric_cols if _is_money_col(c)]
        num_cols = [c for c in numeric_cols if c not in money_cols]
        chrome.flat_table(df, key=key, money_cols=money_cols, num_cols=num_cols)


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
        _render_rows(step.rows, key=key)
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

_EXAMPLE_PROMPTS = (
    "What did I spend last month?",
    "Which service grew the most month over month?",
    "How much recoverable spend is there right now?",
)


async def render() -> None:
    await ui.context.client.connected()  # general/tab storage are only ready after this resolves
    if ui.context.client.is_deleted:
        # connected() also returns for a client NiceGUI pruned while it was still
        # waiting for a socket that never showed up (e.g. a prefetch/crawler GET,
        # or a tab that never opened its websocket) — not a real page load, so
        # there's no client-bound storage (app.storage.tab) left to render into.
        return

    chrome.section_title("Chat")
    chrome.section_caption(
        "Ask about your spend using your own LLM API key — sent straight to the "
        "provider you choose, never stored in plain text by Flashlight."
    )

    def _load_key_for(provider: str) -> str:
        """Same-tab edits (if a keychain write failed) win over the persisted value."""
        return app.storage.tab.get(f"chat_api_key:{provider}", "") or chat_credentials.load_api_key(
            provider
        ) or ""

    def _apply_preset(label: str | None) -> None:
        preset = _PRESETS.get(label or "")
        if preset is not None:
            model_input.value = preset["model"]
            base_url_input.value = preset["base_url"]

    def _on_provider_change(label: str | None) -> None:
        _apply_preset(label)
        api_key_input.value = _load_key_for(label or "")

    with ui.dialog() as settings_dialog, ui.card().classes("gap-3 p-5").style("width:420px"):
        ui.label("Chat settings").classes("text-base font-semibold").style(
            f"color:{chrome.INK_PRIMARY}"
        )
        ui.label(
            "Pick a provider to prefill the model, or choose Custom for a "
            "self-hosted / OpenAI-compatible endpoint. Provider/model/base URL are "
            "remembered on this machine; the key is stored in your OS keychain."
        ).classes("text-xs").style(f"color:{chrome.INK_MUTED}")

        # provider_select's on_change is wired up AFTER model_input/base_url_input/
        # api_key_input exist (below), not passed as a constructor kwarg here:
        # bind_value's initial sync (pulling a value already persisted from a prior
        # run out of app.storage.general) fires on_change synchronously, during
        # this very statement — while model_input et al. are still unassigned in
        # this enclosing scope, which crashed with "cannot access free variable
        # 'model_input'" for anyone with a previously-saved chat_provider.
        provider_select = (
            ui.select(
                list(_PRESETS),
                label="Provider",
                value=_DEFAULT_PROVIDER,
            )
            .classes("w-full")
            .bind_value(app.storage.general, "chat_provider")
            .mark("chat-provider")
        )
        model_input = (
            ui.input("Model")
            .classes("w-full")
            .bind_value(app.storage.general, "chat_model")
            .mark("chat-model")
        )
        base_url_input = (
            ui.input("Base URL (Databricks workspace, or self-hosted/custom)")
            .classes("w-full")
            .bind_value(app.storage.general, "chat_base_url")
            .mark("chat-base-url")
        )
        api_key_input = (
            ui.input("API key").props("type=password").classes("w-full").mark("chat-api-key")
        )
        provider_select.on_value_change(lambda e: _on_provider_change(e.value))
        if not model_input.value:  # first time anything has been configured — prefill
            _apply_preset(provider_select.value)
        api_key_input.value = _load_key_for(provider_select.value)

        def _save_settings() -> None:
            provider, key = provider_select.value, api_key_input.value
            if key:
                app.storage.tab[f"chat_api_key:{provider}"] = key
                if not chat_credentials.save_api_key(provider, key):
                    ui.notify(
                        "Couldn't reach your OS keychain — your key will only last this "
                        f"browser tab. Set {chat_credentials.ENV_VAR} to persist it instead.",
                        type="warning",
                        timeout=8000,
                    )
            settings_dialog.close()

        with ui.row().classes("w-full justify-end"):
            ui.button("Done", on_click=_save_settings).props(
                "flat no-caps color=primary"
            ).mark("chat-settings-done")

    with ui.row().classes("w-full items-center justify-between"):
        status_label = ui.label().classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        with ui.row().classes("items-center gap-1"):
            ui.button(icon="insights", on_click=lambda: ui.navigate.to("/usage")).props(
                "flat round dense"
            ).tooltip("Usage").mark("chat-usage-button")
            ui.button(icon="settings", on_click=settings_dialog.open).props(
                "flat round dense"
            ).mark("chat-settings-button")

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

    with chrome.panel().style("height:65vh; display:flex; flex-direction:column; padding:0;"):
        with ui.scroll_area().classes("w-full").style("flex:1; padding:20px;") as scroll_area:
            with ui.column().classes("w-full max-w-3xl mx-auto gap-3") as message_area:
                placeholder = ui.column().classes("w-full items-center gap-2").style(
                    "padding-top:15vh;"
                )
                with placeholder:
                    ui.icon("bolt", size="2rem").style(f"color:{chrome.INK_MUTED}")
                    ui.label("Ask anything about your spend").classes("text-sm").style(
                        f"color:{chrome.INK_SECONDARY}"
                    )
                    for prompt in _EXAMPLE_PROMPTS:
                        ui.chip(prompt, on_click=lambda p=prompt: _use_example(p)).props(
                            "outline"
                        ).style(f"color:{chrome.INK_SECONDARY};border-color:{chrome.BORDER}")

        def _use_example(prompt: str) -> None:
            input_box.value = prompt

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
            placeholder.set_visibility(False)
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
                for i, step in enumerate(result.steps):
                    _render_tool_step(step, key=f"chat-step-{turn_counter}-{i}")
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
                            ).mark(f"chat-option-{turn_counter}-{i}")
            _scroll_down()

        async def _send_option(option: str) -> None:
            # Clicking an option we offered resolves the ambiguity by
            # construction — flagged so the engine can't answer it with yet
            # another clarifying question (confirmed live: a weak model asked
            # "which month?" straight after the user picked an option that
            # already said "for the previous month (default)").
            input_box.value = option
            await send(answering_clarification=True)

        with ui.row().classes("w-full gap-2 items-center pt-3 px-4 pb-4"):
            input_box = (
                ui.input(placeholder="Ask about your spend...")
                .props("outlined dense rounded")
                .classes("flex-1 text-base")
                .style("font-size:15px;")
                .on("keydown.enter", send)
                .mark("chat-question")
            )
            send_button = (
                ui.button(icon="send", on_click=send).props("round color=primary").mark("chat-send")
            )
