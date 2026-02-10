from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BundleInfo(BaseModel):
    name: str
    path: str  # path as string for serialization
    environment: str | None = None


class DABBundle(BaseModel):
    name: str
    path: str
    raw_config: dict[str, Any] = Field(default_factory=dict)
    environments: list[str] = Field(default_factory=list)


class DABJobConfig(BaseModel):
    key: str
    name: str
    file_path: str
    cluster_key: str | None = None
    schedule: dict[str, Any] | None = None
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    raw_config: dict[str, Any] = Field(default_factory=dict)


class DABClusterConfig(BaseModel):
    key: str
    name: str
    file_path: str
    node_type_id: str | None = None
    num_workers: int | None = None
    autoscale: dict[str, int] | None = None
    spark_conf: dict[str, str] = Field(default_factory=dict)
    raw_config: dict[str, Any] = Field(default_factory=dict)


class DABDiff(BaseModel):
    file_path: str
    original_content: str
    modified_content: str
    description: str
