"""drop key_prefix from api_keys

Revision ID: 004
Revises: 003
Create Date: 2026-02-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("api_keys", "key_prefix")


def downgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "key_prefix",
            sqlmodel.sql.sqltypes.AutoString(length=8),
            nullable=True,
        ),
    )
