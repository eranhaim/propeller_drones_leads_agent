"""SQLAlchemy ORM models for leads and their message history."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class FamiliarityLevel(str, enum.Enum):
    unknown = "unknown"
    beginner = "beginner"
    aware = "aware"
    experienced = "experienced"


class FunnelStage(str, enum.Enum):
    new = "new"
    engaged = "engaged"
    warm = "warm"
    ready_for_call = "ready_for_call"
    handed_off = "handed_off"


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("phone", name="uq_leads_phone"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    familiarity_level: Mapped[FamiliarityLevel] = mapped_column(
        Enum(FamiliarityLevel, name="familiarity_level"),
        default=FamiliarityLevel.unknown,
        nullable=False,
    )
    funnel_stage: Mapped[FunnelStage] = mapped_column(
        Enum(FunnelStage, name="funnel_stage"),
        default=FunnelStage.new,
        nullable=False,
    )

    videos_sent: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    lead_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now(), nullable=False
    )
    last_message_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    messages: Mapped[List["Message"]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Lead id={self.id} phone={self.phone} "
            f"familiarity={self.familiarity_level.value} stage={self.funnel_stage.value}>"
        )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="message_role"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    msg_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now(), nullable=False
    )

    lead: Mapped[Lead] = relationship(back_populates="messages")
