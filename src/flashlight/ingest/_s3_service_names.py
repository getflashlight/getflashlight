"""FOCUS ServiceName values Amazon S3 charges land under.

The S3 sibling of :mod:`flashlight.ingest._redshift_service_names`, and shared the
same three ways so the parts can't drift: ``aws_focus.py`` uses it as part of the
default ``include_services`` allow-list *and* to gate the S3 half of the
``x_cost_subcategory`` classifier *and* to scope ``fetch_efficiency``'s
intelligent-tiering signal; ``transform/sql/065_gold_storage.sql`` hard-codes the
same value (pinned by a drift test, since a static ``.sql`` file can't import).

S3 is in the default pull because the Databricks backing-storage view needs it:
Databricks' own bill covers DBU compute only, so the storage behind a Unity Catalog
managed location is billed by AWS under this ServiceName and is invisible without
it. Transform keeps those rows out of aws.* GOLD (``silver.focus_provider_bill``)
and surfaces mapped buckets as Databricks Storage in ``storage.backing_storage_month``.
See ``docs/design/backing-storage.md``.

Lives directly under ``ingest/`` rather than ``ingest/connectors/`` for the same
reason its Redshift sibling does — so ``config.py`` can import it without triggering
``connectors/__init__.py``, which imports ``config`` back.
"""

from __future__ import annotations

S3_SERVICE_NAMES: frozenset[str] = frozenset({"Amazon Simple Storage Service"})
