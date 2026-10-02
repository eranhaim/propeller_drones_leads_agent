"""Shared LeadMe classification rules.

Customer-confirmed mapping:
    L1 = organic source/campaign, or content that the bot delivered and the
         lead meaningfully engaged with afterwards.
    L2 = a meaningful WhatsApp reply that is not L1.
    L3 = no meaningful WhatsApp reply.
    not-interested = an explicit opt-out; it is outside L1/L2/L3.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Iterable, Optional

from app.config import get_settings
from app.db.models import Lead, Message, MessageRole


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


@dataclass(frozen=True)
class EngagementDecision:
    level: Optional[int]
    reason: str


def _parse_iso(value: object) -> Optional[datetime]:
    """Parse an ISO timestamp into a tz-aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_organic(source: object, campaign_id: object) -> bool:
    """Return whether the exact configured LeadMe source or campaign is organic."""
    settings = get_settings()
    normalized_source = _normalize(source)
    if normalized_source and normalized_source in {
        _normalize(item) for item in settings.leadme_organic_sources
    }:
        return True
    normalized_campaign = _normalize(campaign_id)
    if normalized_campaign and normalized_campaign in {
        _normalize(item) for item in settings.leadme_organic_campaigns
    }:
        return True
    return False


def is_organic_source(lead: Lead) -> bool:
    """Return whether the lead's recorded LeadMe source or campaign is organic."""
    md = lead.lead_metadata or {}
    return is_organic(
        md.get("leadme_source"),
        md.get("leadme_campaign_id"),
    )


def has_content_reply(lead: Lead, messages: Iterable[Message]) -> bool:
    """Return whether the lead replied after the bot delivered content.

    A delivered video/webinar plus meaningful follow-up is the persisted
    evidence available for the customer's "watched our content" L1 rule.
    """
    md = lead.lead_metadata or {}
    sent_times = [
        parsed
        for parsed in (
            _parse_iso(md.get("webinar_sent_at")),
            _parse_iso(md.get("video_sent_at")),
        )
        if parsed is not None
    ]
    if not sent_times:
        return False
    content_sent_at = min(sent_times)
    for message in messages:
        if message.role != MessageRole.user:
            continue
        created_at = message.created_at
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at > content_sent_at:
            return True
    return False


def _has_meaningful_reply(lead: Lead, messages: list[Message]) -> bool:
    user_messages = [message for message in messages if message.role == MessageRole.user]
    if not user_messages:
        return False

    # Meta click-to-WhatsApp ads arrive as the lead's first WhatsApp message.
    # The first detected campaign text is an ad prefill, not a human reply.
    if (lead.lead_metadata or {}).get("ctwa_campaign") and len(user_messages) == 1:
        return False
    return True


def _messages_in_current_session(lead: Lead, messages: list[Message]) -> list[Message]:
    reset_at = _parse_iso((lead.lead_metadata or {}).get("session_reset_at"))
    if reset_at is None:
        return messages
    current_messages: list[Message] = []
    for message in messages:
        created_at = message.created_at
        if created_at is None:
            current_messages.append(message)
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at > reset_at:
            current_messages.append(message)
    return current_messages


def classify_engagement(
    lead: Lead,
    messages: Optional[Iterable[Message]] = None,
) -> EngagementDecision:
    """Return the sole deterministic classification decision for a lead."""
    md = lead.lead_metadata or {}
    if md.get("leadme_relevance") == "not_relevant":
        return EngagementDecision(None, "explicit_not_interested")

    lead_messages = _messages_in_current_session(
        lead,
        list(messages if messages is not None else (lead.messages or [])),
    )
    if is_organic_source(lead):
        return EngagementDecision(1, "organic_source")
    if _has_meaningful_reply(lead, lead_messages):
        if has_content_reply(lead, lead_messages):
            return EngagementDecision(1, "content_consumed")
        if md.get("leadme_reentry_at"):
            return EngagementDecision(2, "reentry_activity")
        return EngagementDecision(2, "meaningful_reply")
    if md.get("leadme_reentry_at"):
        return EngagementDecision(2, "reentry_activity")
    if (lead.lead_metadata or {}).get("ctwa_campaign"):
        return EngagementDecision(3, "ctwa_no_reply")
    return EngagementDecision(3, "no_reply")
