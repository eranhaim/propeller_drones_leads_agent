"""Repository helpers for reading and updating leads and messages."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    FamiliarityLevel,
    FunnelStage,
    Lead,
    Message,
    MessageRole,
)


def get_or_create_lead(session: Session, phone: str, name: Optional[str] = None) -> Lead:
    """Return the lead for ``phone``, creating a new row if needed."""
    lead = session.execute(
        select(Lead).where(Lead.phone == phone)
    ).scalar_one_or_none()

    if lead is None:
        lead = Lead(
            phone=phone,
            name=name,
            familiarity_level=FamiliarityLevel.unknown,
            funnel_stage=FunnelStage.new,
            videos_sent=[],
            lead_metadata={},
        )
        session.add(lead)
        session.flush()
    elif name and not lead.name:
        lead.name = name

    return lead


def add_message(
    session: Session,
    lead: Lead,
    role: MessageRole,
    content: str,
    metadata: Optional[dict] = None,
) -> Message:
    msg = Message(
        lead_id=lead.id,
        role=role,
        content=content,
        msg_metadata=metadata or {},
    )
    session.add(msg)
    lead.last_message_at = datetime.now(timezone.utc)
    session.flush()
    return msg


def recent_messages(session: Session, lead: Lead, limit: int = 30) -> List[Message]:
    """Return the last ``limit`` messages for a lead, oldest first."""
    stmt = (
        select(Message)
        .where(Message.lead_id == lead.id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    msgs = list(session.execute(stmt).scalars().all())
    msgs.reverse()
    return msgs


def update_familiarity(session: Session, lead: Lead, level: FamiliarityLevel) -> None:
    lead.familiarity_level = level
    session.flush()


def update_funnel_stage(session: Session, lead: Lead, stage: FunnelStage) -> None:
    lead.funnel_stage = stage
    session.flush()


def mark_video_sent(session: Session, lead: Lead, video_id: str) -> None:
    sent = list(lead.videos_sent or [])
    if video_id not in sent:
        sent.append(video_id)
        lead.videos_sent = sent
        session.flush()
