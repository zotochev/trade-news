"""token unlocks

Revision ID: b7d2e41c9a10
Revises: af00c6a4fa19
Create Date: 2026-10-12 07:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import trade_news.db.schema

revision: str = "b7d2e41c9a10"
down_revision: str | None = "af00c6a4fa19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "token_unlocks",
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column("unlock_at", trade_news.db.schema.UTCDateTime(timezone=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("coin", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("pct_unlocked", sa.Float(), nullable=False),
        sa.Column("recipients", sa.Text(), nullable=True),
        sa.Column("fetched_at", trade_news.db.schema.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("protocol", "unlock_at", name=op.f("pk_token_unlocks")),
    )
    with op.batch_alter_table("token_unlocks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_token_unlocks_unlock_at"), ["unlock_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("token_unlocks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_token_unlocks_unlock_at"))
    op.drop_table("token_unlocks")
