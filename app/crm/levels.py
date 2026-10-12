"""The single engagement-classification rule for LeadMe.

Levels are numerically inverted: ``1`` is the most engaged, ``3`` the least.
The customer redefined them on 2026-10-12 around how much a lead already
knows, not around how much they chatted:

* **Level 1** -- a deliberately small group. The lead already understands the
  drone industry and who Propeller are, so the sales call is only "why now".
  That is: an organic arrival, the website form, a call to the number
  published on the website, a lead who is back in the system after leaving
  details before, or a lead who has had a long *recent* conversation with the
  bot and engaged with the videos it sent.
* **Level 2** -- reasonable interest. A real conversation with the bot:
  questions, objections, and **booking a call**. Booking is explicitly NOT
  Level 1; leads book calls without knowing what FPV is or who we are.
* **Level 3** -- the starting point for everyone else, paid advertising
  included, and where a lead who never meaningfully replies stays.

Engagement decays: a long conversation from a month ago no longer carries a
lead at Level 1. The source signals do not decay -- they describe where the
lead came from, not what they did this week.

Explicit opt-outs are outside the numeric levels (``level is None``); they get
the configured terminal status instead.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Iterable, Optional

from app.config import get_settings
from app.db.models import Lead, Message, MessageRole


@dataclass(frozen=True)
class EngagementDecision:
    level: Optional[int]
    reason: str


_OPT_OUT_RE = re.compile(
    r"(?:לא\s+רלוונטי|לא\s+תודה|תודה\s+אבל\s+לא|לא\s+מעוניי[נן]|"
    r"תפסיקו|תפסיק|תסירו|הסירו|להסיר\s+אותי)"
)
_L2_QUESTION_RE = re.compile(
    r"(?:מחיר|כמה\s+עולה|עלות|קורס|לימוד|הכשרה|רישיון|רישוי|רת[אא]|"
    r"תיאוריה|מעשי)"
)
_BOOKING_RE = re.compile(
    r"(?:רוצה\s+שיחה|אשמח\s+לשיחה|תקבע|לקבוע|דברו\s+איתי|תחזרו\s+אלי)"
)
_WORD_RE = re.compile(r"[\u0590-\u05FFa-zA-Z0-9]{2,}")


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _matches_any(value: object, configured: Iterable[str]) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return False
    return normalized in {_normalize(item) for item in configured}


def _parse_iso(value: object) -> Optional[datetime]:
    """Parse an ISO timestamp into a tz-aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _is_explicit_opt_out(text: str) -> bool:
    return bool(_OPT_OUT_RE.search(" ".join((text or "").casefold().split())))


def _is_ctwa_prefill(lead: Lead, messages: list[Message]) -> bool:
    return bool((lead.lead_metadata or {}).get("ctwa_campaign")) and len(messages) == 1


def _actual_user_messages(lead: Lead, messages: list[Message]) -> list[Message]:
    """Return the lead's own messages, ignoring a paid ad's prefilled text."""
    user_messages = [message for message in messages if message.role == MessageRole.user]
    return [] if _is_ctwa_prefill(lead, user_messages) else user_messages


def _message_text(message: Message) -> str:
    return " ".join((message.content or "").casefold().split())


def _is_substantive(message: Message) -> bool:
    text = _message_text(message)
    if not text or _is_explicit_opt_out(text):
        return False
    if _L2_QUESTION_RE.search(text) or _BOOKING_RE.search(text):
        return True
    words = _WORD_RE.findall(text)
    return len(words) >= 3 and text not in {"כן", "סבבה", "אוקיי", "ok"}


def _messages_in_current_session(lead: Lead, messages: list[Message]) -> list[Message]:
    reset_at = _parse_iso((lead.lead_metadata or {}).get("session_reset_at"))
    if reset_at is None:
        return messages
    return [
        message
        for message in messages
        if (_as_utc(message.created_at) or reset_at) > reset_at
    ]


def level_1_source_reason(metadata: dict) -> Optional[str]:
    """Return why this lead's SOURCE alone makes it Level 1, or None.

    These labels come from LeadMe's ``sourceType`` and ``campaign`` fields and
    are account-specific, so the accepted values are configuration rather than
    code. A lead arriving through any of them has already seen Propeller's own
    content, which is the whole point of the tier.
    """
    settings = get_settings()
    source = metadata.get("leadme_source")
    campaign = metadata.get("leadme_campaign_id") or metadata.get("leadme_campaign")

    if (
        metadata.get("leadme_source_is_website_form")
        or _matches_any(source, settings.leadme_website_form_sources)
        or _matches_any(campaign, settings.leadme_website_form_campaigns)
    ):
        return "website_form"
    if _matches_any(source, settings.leadme_level_1_sources):
        return "organic_or_incoming_call"
    if _matches_any(campaign, settings.leadme_level_1_campaigns):
        return "propeller_content_campaign"
    return None


def engaged_with_a_video(lead: Lead, messages: Iterable[Message]) -> bool:
    """Return whether the lead wrote something real after a video was sent.

    GreenAPI reports no play, watch or completion event for media we send, so
    "watched the video" is not observable. The usable proxy is a substantive
    message after the video went out -- a question about it, or a request for
    more.
    """
    sent_at = _parse_iso((lead.lead_metadata or {}).get("video_sent_at"))
    if sent_at is None:
        return False
    for message in messages:
        if message.role != MessageRole.user:
            continue
        created_at = _as_utc(message.created_at)
        if created_at is not None and created_at > sent_at and _is_substantive(message):
            return True
    return False


def _is_fresh(moment: Optional[datetime]) -> bool:
    if moment is None:
        return False
    window = timedelta(days=get_settings().lead_engagement_fresh_days)
    return datetime.now(timezone.utc) - moment <= window


def _last_activity_at(user_messages: list[Message]) -> Optional[datetime]:
    moments = [
        moment
        for moment in (_as_utc(message.created_at) for message in user_messages)
        if moment is not None
    ]
    return max(moments) if moments else None


def _is_deeply_engaged(
    lead: Lead,
    messages: list[Message],
    user_messages: list[Message],
) -> bool:
    """Return whether conversation depth alone earns Level 1.

    All three conditions are required. Message count on its own was what
    pushed shallow leads into Level 1 and is the behaviour the customer
    rejected.
    """
    if len(user_messages) < get_settings().lead_deep_engagement_messages:
        return False
    if not engaged_with_a_video(lead, messages):
        return False
    return _is_fresh(_last_activity_at(user_messages))


def classify_engagement(
    lead: Lead,
    messages: Optional[Iterable[Message]] = None,
) -> EngagementDecision:
    """Return the sole deterministic classification decision for a lead."""
    metadata = lead.lead_metadata or {}
    if metadata.get("leadme_relevance") == "not_relevant":
        return EngagementDecision(None, "explicit_not_interested")

    lead_messages = _messages_in_current_session(
        lead,
        list(messages if messages is not None else (lead.messages or [])),
    )
    user_messages = _actual_user_messages(lead, lead_messages)
    if any(_is_explicit_opt_out(_message_text(message)) for message in user_messages):
        return EngagementDecision(None, "explicit_not_interested")

    source_reason = level_1_source_reason(metadata)
    if source_reason is not None:
        return EngagementDecision(1, source_reason)
    if metadata.get("leadme_reentry_at"):
        return EngagementDecision(1, "returning_lead")
    if _is_deeply_engaged(lead, lead_messages, user_messages):
        return EngagementDecision(1, "deep_recent_engagement")

    if any(_is_substantive(message) for message in user_messages):
        return EngagementDecision(2, "meaningful_reply")
    if metadata.get("ctwa_campaign"):
        return EngagementDecision(3, "paid_ad_no_meaningful_reply")
    return EngagementDecision(3, "no_meaningful_reply")
