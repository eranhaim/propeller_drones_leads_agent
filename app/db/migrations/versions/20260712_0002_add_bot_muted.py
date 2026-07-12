"""Add ``bot_muted`` to leads for per-lead pause / human-takeover.

Revision ID: 20260712_0002
Revises: 20260704_0001
Create Date: 2026-07-12

When True, ``bot_muted`` tells the WhatsApp handler to skip agent
invocation for this lead and tells the follow-up scheduler to skip
sending nudges. Toggled via the admin UI when a human wants to take
over a chat directly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260712_0002"
down_revision: Union[str, None] = "20260704_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column(
            "bot_muted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("leads", "bot_muted")
