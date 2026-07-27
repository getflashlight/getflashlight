"""FOCUS ServiceName values Redshift charges land under.

Shared by ``aws_focus.py`` (tagging ``x_cost_subcategory`` on ingest, and as the
default ``include_services`` allow-list in ``config.py``) and ``redshift.py``
(filtering BRONZE FOCUS rows for ``_cost_breakdown()``) so the two can't drift out
of sync — also imported by the dashboard's Redshift-scoped view. Lives directly
under ``ingest/`` rather than ``ingest/connectors/`` so ``config.py`` can import it
without triggering ``connectors/__init__.py`` (which imports ``config`` back —
that would be circular).
"""

from __future__ import annotations

REDSHIFT_SERVICE_NAMES: frozenset[str] = frozenset({"Amazon Redshift", "Amazon Redshift Spectrum"})
