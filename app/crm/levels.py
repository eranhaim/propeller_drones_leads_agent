"""Shared LeadMe classification rules from the recovered sales evidence.

L1 is a hot lead. A new website form starts there, but weak follow-up must
move it to L2. A non-website lead becomes L1 only after a delivered video and
clear positive engagement. L2 is meaningful middle engagement; L3 is a lead
who never meaningfully replied. Explicit opt-outs are outside the numeric
levels.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Iterable, Optional

from app.db.models import Lead, Message, MessageRole


@dataclass(frozen=True)
class EngagementDecision:
    level: Optional[int]
    reason: str


def allows_website_l1_downgrade(lead: Lead, target_level: int) -> bool:
    """Return whether a website lead may move from its initial L1 to L2."""
    metadata = lead.lead_metadata or {}
    return bool(
        target_level == 2
        and metadata.get("leadme_source_is_website_form")
        and metadata.get("leadme_initial_priority") == 1
    )


_OPT_OUT_RE = re.compile(
    r"(?:לא\s+רלוונטי|לא\s+תודה|תודה\s+אבל\s+לא|לא\s+מעוניי[נן]|"
    r"תפסיקו|תפסיק|תסירו|הסירו|להסיר\s+אותי)"
)
_CONTENT_POSITIVE_RE = re.compile(
    r"(?:ראיתי|צפיתי|אהבתי|מעניין\s+אותי|נשמע\s+טוב|מעולה|"
    r"רוצה\s+להתקדם|אשמח\s+להתקדם|רוצה\s+להירשם|אשמח\s+להירשם|"
    r"זה\s+מתאים\s+לי)"
)
_HOT_WEBSITE_RE = re.compile(
    r"(?:מעניין\s+אותי\s+מאוד|רוצה\s+להתחיל|רוצה\s+להתקדם|"
    r"אשמח\s+להתחיל|רוצה\s+להירשם|אשמח\s+להירשם|מתי\s+מתחיל|"
    r"מוכן\s+להתחיל)"
)
_L2_QUESTION_RE = re.compile(
    r"(?:מחיר|כמה\s+עולה|עלות|קורס|לימוד|הכשרה|רישיון|רישוי|רת[אא]|"
    r"תיאוריה|מעשי)"
)
_SHALLOW_BOOKING_RE = re.compile(
    r"(?:רוצה\s+שיחה|אשמח\s+לשיחה|תקבע|לקבוע|דברו\s+איתי|תחזרו\s+אלי)"
)
_WORD_RE = re.compile(r"[\u0590-\u05FFa-zA-Z0-9]{2,}")


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


def _is_explicit_opt_out(text: str) -> bool:
    return bool(_OPT_OUT_RE.search(" ".join((text or "").casefold().split())))


def _is_ctwa_prefill(lead: Lead, messages: list[Message]) -> bool:
    return bool((lead.lead_metadata or {}).get("ctwa_campaign")) and len(messages) == 1


def _actual_user_messages(lead: Lead, messages: list[Message]) -> list[Message]:
    user_messages = [message for message in messages if message.role == MessageRole.user]
    return [] if _is_ctwa_prefill(lead, user_messages) else user_messages


def _message_text(message: Message) -> str:
    return " ".join((message.content or "").casefold().split())


def _is_substantive(message: Message) -> bool:
    text = _message_text(message)
    if not text or _is_explicit_opt_out(text):
        return False
    if _L2_QUESTION_RE.search(text) or _SHALLOW_BOOKING_RE.search(text):
        return True
    words = _WORD_RE.findall(text)
    return len(words) >= 3 and text not in {"כן", "סבבה", "אוקיי", "ok"}


def has_content_reply(lead: Lead, messages: Iterable[Message]) -> bool:
    """Return whether delivered content got a clear positive response.

    A bare message after a video is not evidence that the lead watched or
    cared about it. The recovered sales rules require positive/meaningful
    follow-up after delivery before a video can make the lead L1.
    """
    md = lead.lead_metadata or {}
    sent_times = [
        parsed
        for parsed in (_parse_iso(md.get("video_sent_at")),)
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
        text = _message_text(message)
        if created_at > content_sent_at and _CONTENT_POSITIVE_RE.search(text):
            return True
    return False


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
    user_messages = _actual_user_messages(lead, lead_messages)
    if any(_is_explicit_opt_out(_message_text(message)) for message in user_messages):
        return EngagementDecision(None, "explicit_not_interested")

    if has_content_reply(lead, lead_messages):
        return EngagementDecision(1, "positive_content_followup")

    if md.get("leadme_reentry_at"):
        return EngagementDecision(2, "reentry_activity")

    website_form = bool(md.get("leadme_source_is_website_form"))
    if website_form:
        if not user_messages:
            return EngagementDecision(1, "website_initial")
        if any(_L2_QUESTION_RE.search(_message_text(message)) for message in user_messages):
            return EngagementDecision(2, "website_question_without_l1_signal")
        if any(_SHALLOW_BOOKING_RE.search(_message_text(message)) for message in user_messages):
            return EngagementDecision(2, "website_shallow_booking")
        if any(_HOT_WEBSITE_RE.search(_message_text(message)) for message in user_messages):
            return EngagementDecision(1, "website_hot_followup")
        return EngagementDecision(2, "website_weak_followup")

    if any(_is_substantive(message) for message in user_messages):
        return EngagementDecision(2, "meaningful_reply")
    if md.get("ctwa_campaign"):
        return EngagementDecision(3, "ctwa_no_reply")
    return EngagementDecision(3, "no_reply")
