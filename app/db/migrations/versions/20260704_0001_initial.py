"""Initial schema: leads + messages.

Revision ID: 20260704_0001
Revises:
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260704_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    familiarity_enum = sa.Enum(
        "unknown", "beginner", "aware", "experienced",
        name="familiarity_level",
    )
    funnel_enum = sa.Enum(
        "new", "engaged", "warm", "ready_for_call", "handed_off",
        name="funnel_stage",
    )
    role_enum = sa.Enum(
        "user", "assistant", "system", "tool",
        name="message_role",
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column(
            "familiarity_level", familiarity_enum,
            nullable=False, server_default="unknown",
        ),
        sa.Column(
            "funnel_stage", funnel_enum,
            nullable=False, server_default="new",
        ),
        sa.Column("videos_sent", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("lead_metadata", sa.JSON, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("last_message_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("phone", name="uq_leads_phone"),
    )
    op.create_index("ix_leads_phone", "leads", ["phone"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "lead_id", sa.Integer,
            sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("msg_metadata", sa.JSON, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_messages_lead_id", "messages", ["lead_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_lead_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_leads_phone", table_name="leads")
    op.drop_table("leads")
    sa.Enum(name="message_role").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="funnel_stage").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="familiarity_level").drop(op.get_bind(), checkfirst=True)
