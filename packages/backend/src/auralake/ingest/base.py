"""Connector contract. Every source maps its native billing into FOCUS rows."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from auralake.focus.model import FocusRecord


@dataclass(frozen=True)
class IngestWindow:
    """Inclusive date range to ingest [start, end]."""

    start: date
    end: date


class Connector(ABC):
    """A billing source. Implementations are stateless per ``fetch`` call."""

    #: Stable identifier, stamped onto every row as ``x_source_connector``.
    name: str = "base"

    @abstractmethod
    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        """Yield canonical FOCUS records for the window.

        Implementations must stamp ``x_source_connector = self.name`` on each
        record (or rely on the runner to assert it) and pick FOCUS-valid
        controlled-vocabulary values, falling back to ``OTHER`` rather than
        inventing new ones.
        """
        raise NotImplementedError
