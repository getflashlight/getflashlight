"""ChatService — orchestrates the agentic tool-calling loop via Databricks Model Serving.

Credentials are pulled from the default Databricks workspace configured in the
database (via ``app.state.config``), so no separate LLM env vars are needed.
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
from auralake_shared.models.config import AuraLakeConfig, DatabricksWorkspaceConfig
from openai import APIError, OpenAI
from sqlmodel import Session

from auralake_backend.server.chat.schemas import (
    ChartData,
    ChatMessage,
    ChatResponse,
    ToolCallInfo,
)
from auralake_backend.server.chat.tools import TOOL_DEFINITIONS, TOOL_DISPATCH

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You are Auralake Assistant, an expert in lakehouse cost optimization.
You have access to tools that query a PostgreSQL database containing Databricks
billing, compute resources, jobs, Delta tables, S3 inventory, and optimization
recommendations.

Guidelines:
- Always use tools to answer questions about the user's data. Never guess or speculate.
- When presenting cost data, use USD formatting with 2 decimal places.
- Always call `create_visualization` when answering questions about data — cost
  breakdowns, trends, comparisons, distributions, etc. Pick the best chart type
  for the data: line for trends over time, bar for comparisons/rankings, pie for
  proportional breakdowns, heatmap for cross-dimensional patterns (e.g. cost by
  SKU × month), scatter for correlations, area for cumulative trends. Never skip
  the chart on data questions.
- Keep answers concise and actionable. Highlight key findings and recommendations.
- If no data is available for a query, say so clearly.
"""

MAX_TOOL_ROUNDS = 5


def _normalize_chart_data(raw: Any) -> dict[str, list[Any]]:
    """Convert LLM chart data to column-oriented format.

    The LLM may send either:
      - column-oriented: {"date": ["a","b"], "cost": [1,2]}  (expected)
      - row-oriented:    [{"date": "a", "cost": 1}, {"date": "b", "cost": 2}]

    This normalizes both to column-oriented.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        keys = raw[0].keys()
        return {k: [row.get(k) for row in raw] for k in keys}
    return {}
MODEL = os.environ.get("AURALAKE_CHAT_MODEL", "databricks-gpt-5-nano")


def _resolve_workspace(config: AuraLakeConfig) -> DatabricksWorkspaceConfig:
    """Return the default Databricks workspace from config."""
    workspaces = config.databricks.workspaces
    for ws in workspaces.values():
        if ws.is_default:
            return ws
    if workspaces:
        return next(iter(workspaces.values()))
    raise RuntimeError(
        "No Databricks workspace configured. "
        "Add a Databricks connection via Settings or POST /api/v1/connections."
    )


class ChatService:
    """Stateless service that handles a single chat request."""

    def __init__(self, session: Session, config: AuraLakeConfig) -> None:
        self._session = session
        ws = _resolve_workspace(config)
        if not ws.token:
            raise RuntimeError(
                "Default Databricks workspace has no token. "
                "Update the connection with a valid personal access token."
            )
        base_url = f"{ws.host.rstrip('/')}/serving-endpoints"
        self._client = OpenAI(api_key=ws.token, base_url=base_url)

    def handle(self, message: str, history: list[ChatMessage]) -> ChatResponse:
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})
        messages.append({"role": "user", "content": message})

        tool_calls_info: list[ToolCallInfo] = []
        charts: list[ChartData] = []

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                response = self._client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    max_tokens=5000,
                )

                choice = response.choices[0]

                # If the model produced a final text answer, we're done
                if choice.finish_reason == "stop":
                    return ChatResponse(
                        answer=choice.message.content or "",
                        tool_calls=tool_calls_info,
                        charts=charts,
                    )

                # Process tool calls
                if choice.message.tool_calls:
                    # Append the assistant message with tool calls
                    messages.append(choice.message.model_dump())

                    for tc in choice.message.tool_calls:
                        fn_name = tc.function.name
                        fn_args = json.loads(tc.function.arguments)

                        logger.info(
                            "chat_tool_call",
                            tool=fn_name,
                            args=fn_args,
                        )

                        tool_calls_info.append(
                            ToolCallInfo(tool_name=fn_name, parameters=fn_args)
                        )

                        result = self._execute_tool(fn_name, fn_args)

                        # Capture chart specs from create_visualization calls
                        if fn_name == "create_visualization":
                            charts.append(
                                ChartData(
                                    chart_type=fn_args.get("chart_type", "bar"),
                                    title=fn_args.get("title", ""),
                                    data=_normalize_chart_data(fn_args.get("data", {})),
                                    x=fn_args.get("x", ""),
                                    y=fn_args.get("y", ""),
                                )
                            )

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": json.dumps(result, default=str),
                            }
                        )
                else:
                    # No tool calls and not stop — treat as final answer
                    return ChatResponse(
                        answer=choice.message.content or "",
                        tool_calls=tool_calls_info,
                        charts=charts,
                    )

            # Exhausted max rounds — make one final call without tools
            response = self._client.chat.completions.create(
                model=MODEL,
                messages=messages,
                max_tokens=5000,
            )
            return ChatResponse(
                answer=response.choices[0].message.content or "",
                tool_calls=tool_calls_info,
                charts=charts,
            )
        except APIError as exc:
            logger.error("chat_llm_error", status=exc.status_code, message=str(exc))
            return ChatResponse(
                answer=(
                    "Sorry, I'm having trouble reaching the AI model right now. "
                    f"(Error {exc.status_code}: {exc.message})"
                ),
                tool_calls=tool_calls_info,
                charts=charts,
            )

    def _execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = TOOL_DISPATCH.get(name)
        if fn is None:
            return {"error": f"Unknown tool: {name}"}

        try:
            if name == "create_visualization":
                return fn(**args)
            return fn(self._session, **args)
        except Exception as exc:
            logger.error("chat_tool_error", tool=name, error=str(exc))
            return {"error": str(exc)}
