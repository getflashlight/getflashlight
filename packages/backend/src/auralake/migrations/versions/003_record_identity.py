"""Add x_record_id / x_record_type to raw.focus_record.

Source-record identity for the Databricks append-only correction model
(ORIGINAL / RETRACTION / RESTATEMENT). Both are part of the dedupe key so the
correction records survive as distinct rows and net out via SUM downstream.

Revision ID: 003_record_identity
Revises: 002_effective_is_list
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_record_identity"
down_revision: str | None = "002_effective_is_list"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("focus_record", sa.Column("x_record_id", sa.String, nullable=True), schema="raw")
    op.add_column(
        "focus_record", sa.Column("x_record_type", sa.String, nullable=True), schema="raw"
    )


def downgrade() -> None:
    op.drop_column("focus_record", "x_record_type", schema="raw")
    op.drop_column("focus_record", "x_record_id", schema="raw")
