"""Pydantic request/response models for the chat endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class ChartData(BaseModel):
    chart_type: str  # "bar", "line", "pie", "area"
    title: str
    data: dict[str, list[Any]]  # column_name → values
    x: str
    y: str | list[str]


class ToolCallInfo(BaseModel):
    tool_name: str
    parameters: dict[str, Any]


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[ToolCallInfo] = []
    charts: list[ChartData] = []
