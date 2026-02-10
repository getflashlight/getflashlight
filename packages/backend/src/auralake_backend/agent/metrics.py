"""Stage-level metrics for Spark jobs."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StageMetrics:
    """Metrics for a single Spark stage."""

    stage_id: int
    stage_name: str = ""
    num_tasks: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    shuffle_read_bytes: int = 0
    shuffle_write_bytes: int = 0
    spill_bytes: int = 0
    duration_ms: int = 0
    records_read: int = 0
    records_written: int = 0


@dataclass
class QueryMetrics:
    """Aggregated metrics for a query execution."""

    query_id: str
    total_duration_ms: int = 0
    total_rows_scanned: int = 0
    total_bytes_read: int = 0
    total_shuffle_bytes: int = 0
    total_spill_bytes: int = 0
    stages: list[StageMetrics] = field(default_factory=list)

    @property
    def has_spill(self) -> bool:
        return self.total_spill_bytes > 0

    @property
    def has_heavy_shuffle(self) -> bool:
        return (
            self.total_shuffle_bytes > self.total_bytes_read * 0.5
            if self.total_bytes_read
            else False
        )
