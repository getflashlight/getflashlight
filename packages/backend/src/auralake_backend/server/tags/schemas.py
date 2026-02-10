from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class EnforceRequest(BaseModel):
    tag_key: str
    default_value: str


class S3InventoryStatusResponse(BaseModel):
    configured_buckets: list[str]
    latest_report_date: datetime | None
    total_objects: int
    matched_objects: int
    orphan_objects: int


class S3InventoryObjectResponse(BaseModel):
    bucket: str
    key: str
    size_bytes: int
    matched_table: str | None
    is_orphan: bool
    tags: dict[str, Any]
    collected_at: datetime


class S3InventoryCollectResponse(BaseModel):
    objects_ingested: int
    objects_matched: int
    objects_orphaned: int
    buckets_processed: list[str]
