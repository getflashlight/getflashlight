"""FOCUS ServiceName values Amazon EC2 charges land under.

The compute sibling of :mod:`flashlight.ingest._s3_service_names`, shared the same
way so the parts can't drift: ``aws_focus.py`` uses it as part of the default
``include_services`` allow-list; ``transform/sql/066_gold_compute.sql`` hard-codes
the same value (pinned by a drift test, since a static ``.sql`` file can't import).

EC2 is in the default pull because the Databricks backing-compute view needs it:
Databricks' own bill covers DBU compute only, so the cloud VM underneath a classic
(non-serverless) cluster is billed by AWS under this ServiceName and is invisible
without it. Transform keeps those rows out of aws.* GOLD (``silver.focus_provider_bill``)
and surfaces mapped instances as Databricks Compute in ``compute.backing_compute_month``.
See ``docs/design/backing-compute.md``.

⚠ UNVALIDATED against a live FOCUS export — same caveat class as the S3/Redshift
keyword tables. This repo's own test fixtures disagree with each other on the exact
string ("AmazonEC2", "Amazon Elastic Compute Cloud - Compute"), so confirm the real
FOCUS ``ServiceName`` value for EC2 against a live export before relying on this in
production; correct it here (the one place both the allow-list and the GOLD join
read from) if it's wrong.

Lives directly under ``ingest/`` rather than ``ingest/connectors/`` for the same
reason its S3/Redshift siblings do — so ``config.py`` can import it without
triggering ``connectors/__init__.py``, which imports ``config`` back.
"""

from __future__ import annotations

EC2_SERVICE_NAMES: frozenset[str] = frozenset({"Amazon Elastic Compute Cloud"})
