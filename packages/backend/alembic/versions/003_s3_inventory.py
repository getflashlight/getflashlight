"""s3 inventory objects table

Revision ID: 003
Revises: 002
Create Date: 2026-02-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "s3_inventory_objects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bucket", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("last_modified", sa.DateTime(), nullable=False),
        sa.Column("storage_class", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("etag", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("matched_table", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("matched_table_location", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("is_orphan", sa.Boolean(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("inventory_run_id", sa.Uuid(), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_s3_inventory_objects_bucket_key",
        "s3_inventory_objects",
        ["bucket", "key"],
    )
    op.create_index(
        "ix_s3_inventory_objects_is_orphan",
        "s3_inventory_objects",
        ["is_orphan"],
    )


def downgrade() -> None:
    op.drop_index("ix_s3_inventory_objects_is_orphan", table_name="s3_inventory_objects")
    op.drop_index("ix_s3_inventory_objects_bucket_key", table_name="s3_inventory_objects")
    op.drop_table("s3_inventory_objects")
