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


def recent_messages(
    session: Session,
    lead: Lead,
    limit: int = 30,
    after_dt: Optional[datetime] = None,
) -> List[Message]:
    """Return the last ``limit`` messages for a lead, oldest first.

    If ``after_dt`` is given, only messages created after that timestamp are
    returned (used to hide pre-reset history from the agent without deleting
    anything from the DB).
    """
    stmt = select(Message).where(Message.lead_id == lead.id)
    if after_dt is not None:
        stmt = stmt.where(Message.created_at > after_dt)
    stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
    msgs = list(session.execute(stmt).scalars().all())
    msgs.reverse()
    return msgs


_LEADME_KEYS = frozenset({
    "leadme_campaign_id",
    "leadme_lead_id",
    "leadme_raw_comment",
    "leadme_last_level",
    "opener_campaign_id",
})


def reset_lead_session(session: Session, lead: Lead) -> None:
    """Reset a lead as if they are brand-new while preserving LeadMe history.

    Clears funnel stage, familiarity, videos sent, and all metadata except
    LeadMe identifiers (which must never be lost).  Records the reset time in
    ``lead_metadata["session_reset_at"]`` so ``_history_as_messages`` can
    filter out messages from the previous session.
    """
    lead.familiarity_level = FamiliarityLevel.unknown
    lead.funnel_stage = FunnelStage.new
    lead.videos_sent = []

    old_meta = dict(lead.lead_metadata or {})
    preserved = {k: v for k, v in old_meta.items() if k in _LEADME_KEYS}
    preserved["session_reset_at"] = datetime.now(timezone.utc).isoformat()
    lead.lead_metadata = preserved
    session.flush()


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


def update_lead_metadata(session: Session, lead: Lead, **fields) -> None:
    """Merge non-None values into the lead_metadata JSON blob."""
    filtered = {k: v for k, v in fields.items() if v is not None and v != ""}
    if not filtered:
        return
    merged = dict(lead.lead_metadata or {})
    merged.update(filtered)
    lead.lead_metadata = merged
    session.flush()
