"""The single engagement-classification rule for LeadMe.

Levels are numerically inverted: ``1`` is the most engaged, ``3`` the least.
The customer redefined them on 2026-10-12 around how much a lead already
knows, not around how much they chatted:

* **Level 1** -- the lead already understands the drone industry and who
  Propeller are, so the sales call is only "why now". Either of two routes
  gets them there:

  - **Where they came from**: an organic arrival, a call to the number
    published on the website, the website form, a landing page, the webinar,
    or a lead who is back in the system after leaving details before. This
    comes from the lead's LeadMe tags, pulled by
    :mod:`app.crm.leadme_source`, because LeadMe never sends it to us.
  - **What they did**: they consumed Propeller's content *and* booked a call
    for a specific window. Both halves are required, and both must be recent.

* **Level 2** -- the lead booked a call but the sales call still has to
  explain the industry: a booking without the content, without a specific
  window, or a stale one. A booked call is the whole of what Level 2 means.
* **Level 3** -- the starting point for everyone else, paid advertising
  included. A lead who never meaningfully replies stays here, and so does one
  who held a real conversation but never booked anything
  (``conversation_no_booking``), because the customer's level 3 says
  "לא תיאם שיחה" in as many words. That is the majority of the book.

Engagement decays; where a lead came from does not. A booking from a month
ago no longer carries a lead at Level 1 -- it falls back to Level 2. The
source signals never decay, because they describe what the lead already knew
before we spoke to them.

Pairing content with a booking is the customer's final word on the model
(Roy and Omer, 2026-10-12), after a day in which a booking alone was first
Level 2 and then Level 1. Neither signal is sufficient by itself: leads book
calls without knowing what FPV is, and leads watch a video without ever
intending to buy.

Explicit opt-outs are outside the numeric levels (``level is None``); they get
the configured terminal status instead.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Iterable, Optional

from app.config import get_settings
from app.db.models import FunnelStage, Lead, Message, MessageRole


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

# A booking only reaches Level 1 with a real window. ``any`` and ``none`` are
# what schedule_call falls back to when the lead never named one.
_SPECIFIC_SLOTS = frozenset({"9-12", "12-15", "15-18"})
# ``any`` is still a booked call, so it still counts as "תיאם שיחה". ``none``
# records a lead who did not want one, so it does not.
_BOOKED_SLOTS = _SPECIFIC_SLOTS | {"any"}


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _matches_any(value: object, configured: Iterable[str]) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return False
    return normalized in {_normalize(item) for item in configured}


def _tagged(tags: Iterable[str], configured: Iterable[str]) -> bool:
    """Return whether any tag contains any configured value.

    Containment rather than equality, because the tags that matter are
    families: a landing page is "עמוד נחיתה אקדמיה מובייל" or
    "דף נחיתה אוטודרון", and one paid lead carries four separate tags that
    each merely contain "facebook".
    """
    needles = [_normalize(item) for item in configured]
    needles = [needle for needle in needles if needle]
    if not needles:
        return False
    return any(
        needle in _normalize(tag) for tag in tags for needle in needles
    )


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


def came_from_paid_advertising(metadata: dict) -> bool:
    """Return whether this lead is attributable to a paid ad.

    Checked before every Level 1 source route and disqualifying for all of
    them. A Click-To-WhatsApp ad reaches LeadMe with the same ``whatsapp``
    source as a genuine organic arrival; its Facebook tags, or the campaign
    the bot recorded itself, are the only things that tell the two apart.
    """
    settings = get_settings()
    return bool(
        metadata.get("ctwa_campaign")
        or _tagged(metadata.get("leadme_tags") or [], settings.leadme_paid_tags)
        or _matches_any(
            metadata.get("leadme_source_type") or metadata.get("leadme_source"),
            settings.leadme_paid_source_types,
        )
    )


def level_1_source_reason(metadata: dict) -> Optional[str]:
    """Return why this lead's SOURCE alone makes it Level 1, or None.

    Where a lead came from lives in its LeadMe TAGS, pulled by
    ``app/crm/leadme_source.py``. LeadMe's own source field never reaches
    this bot: the external-interface push fires before the field is written,
    and v3 exposes no endpoint that returns it. The sourceType and campaign
    checks below are kept for the payloads that do carry them.

    Every accepted value is configuration, because they are account-specific
    Hebrew labels that the customer edits in the LeadMe UI.
    """
    settings = get_settings()
    tags = metadata.get("leadme_tags") or []
    source = metadata.get("leadme_source_type") or metadata.get("leadme_source")
    campaign = metadata.get("leadme_campaign_id") or metadata.get("leadme_campaign")

    if came_from_paid_advertising(metadata):
        return None

    if (
        _tagged(tags, settings.leadme_webinar_tags)
        or _matches_any(campaign, settings.leadme_webinar_campaigns)
    ):
        return "webinar"
    if (
        _tagged(tags, settings.leadme_incoming_call_tags)
        or _matches_any(source, settings.leadme_incoming_call_source_types)
    ):
        return "incoming_call"
    if (
        metadata.get("leadme_source_is_website_form")
        or _tagged(tags, settings.leadme_website_form_tags)
        or _matches_any(source, settings.leadme_website_form_source_types)
        or _matches_any(source, settings.leadme_website_form_sources)
        or _matches_any(campaign, settings.leadme_website_form_campaigns)
    ):
        return "website_form"
    if (
        _tagged(tags, settings.leadme_landing_page_tags)
        or _matches_any(source, settings.leadme_landing_page_source_types)
        or _matches_any(campaign, settings.leadme_landing_page_campaigns)
    ):
        return "landing_page"
    if _matches_any(source, settings.leadme_organic_source_types):
        return "organic"
    if _matches_any(source, settings.leadme_level_1_sources):
        return "organic_or_incoming_call"
    if _matches_any(campaign, settings.leadme_level_1_campaigns):
        return "propeller_content_campaign"
    return None


def booked_a_call(lead: Lead) -> bool:
    """Return whether a call was actually scheduled for this lead.

    "תיאם שיחה" in the customer's rule, which is Level 2 on its own. Wider
    than :func:`booked_a_specific_window`, and deliberately narrower than the
    funnel stage: the prompt also hands off a job applicant, and that is not
    a booking.
    """
    if lead.funnel_stage != FunnelStage.handed_off:
        return False
    slot = (lead.lead_metadata or {}).get("preferred_call_slot")
    return _normalize(slot) in _BOOKED_SLOTS


def booked_a_specific_window(lead: Lead) -> bool:
    """Return whether the lead booked a call for a concrete time window.

    The customer's rule says "תיאם שיחה בשעה מסוימת" -- booked a call at a
    specific hour -- so a booking that fell back to ``any`` does not qualify.
    ``schedule_call`` books with ``any`` rather than stalling when the lead
    never names a window, and those leads are exactly the vaguer intent the
    rule means to exclude.
    """
    if lead.funnel_stage != FunnelStage.handed_off:
        return False
    slot = (lead.lead_metadata or {}).get("preferred_call_slot")
    return _normalize(slot) in _SPECIFIC_SLOTS


def booked_at(lead: Lead, user_messages: list[Message]) -> Optional[datetime]:
    """Return when the call was booked, as well as we can tell.

    ``booked_at`` has only been recorded since 2026-10-12. For every lead
    booked before that the best available stand-in is their last inbound
    message, which is never earlier than the booking and so can only make an
    old booking look newer than it is -- the forgiving direction.
    """
    recorded = _parse_iso((lead.lead_metadata or {}).get("booked_at"))
    if recorded is not None:
        return recorded
    return _last_activity_at(user_messages)


def received_the_webinar(lead: Lead) -> bool:
    """Return whether this lead has had Propeller's webinar.

    Two ways to know. LeadMe says so, through the webinar tag or campaign --
    which also makes the lead Level 1 on its own, so this path only matters
    if that route is ever narrowed. Or the bot sent it, which nothing does
    today: there is no webinar in ``data/videos.json`` and nothing else in
    the schema records one, so ``WEBINAR_VIDEO_IDS`` is empty and matches
    nobody. Add the webinar to the catalogue and name its id there to switch
    that half on.

    Whether the lead *attended* stays unobservable either way.
    """
    settings = get_settings()
    metadata = lead.lead_metadata or {}
    if _tagged(metadata.get("leadme_tags") or [], settings.leadme_webinar_tags):
        return True
    if _matches_any(
        metadata.get("leadme_campaign_id") or metadata.get("leadme_campaign"),
        settings.leadme_webinar_campaigns,
    ):
        return True
    return any(
        _matches_any(sent, settings.webinar_video_ids)
        for sent in (lead.videos_sent or [])
    )


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


def _received_our_content(
    lead: Lead,
    messages: list[Message],
    user_messages: list[Message],
) -> bool:
    """Return whether the lead has taken in enough to know the industry.

    The customer's three content signals, any one of which counts: they got
    the webinar, they got a video and said something real about it, or the
    conversation itself ran long enough to be an education. None of them is
    sufficient on its own for Level 1 -- the caller pairs this with a booking.
    """
    if received_the_webinar(lead):
        return True
    if engaged_with_a_video(lead, messages):
        return True
    substantive = sum(1 for message in user_messages if _is_substantive(message))
    return substantive >= get_settings().lead_deep_engagement_messages


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

    # Where the lead came from, which never decays.
    source_reason = level_1_source_reason(metadata)
    if source_reason is not None:
        return EngagementDecision(1, source_reason)
    if metadata.get("leadme_reentry_at"):
        return EngagementDecision(1, "returning_lead")

    # What the lead did, which does decay. Content and a booked window
    # together are Level 1; a booked call that misses any part of that is
    # Level 2, and a booked call is now the only thing Level 2 means.
    if booked_a_call(lead):
        if not booked_a_specific_window(lead):
            return EngagementDecision(2, "booking_without_a_window")
        if not _is_fresh(booked_at(lead, user_messages)):
            return EngagementDecision(2, "stale_booking")
        if _received_our_content(lead, lead_messages, user_messages):
            return EngagementDecision(1, "content_and_booking")
        return EngagementDecision(2, "booking_without_content")

    # A real conversation that never reached a booking. Roy's rule lists
    # "לא תיאם שיחה" under level 3 explicitly, so chatting with the bot, on
    # its own, no longer lifts a lead off the starting level. This is the one
    # line to change if the sales team later wants those leads back at Level
    # 2; it moves roughly 307 of 571 leads.
    if any(_is_substantive(message) for message in user_messages):
        return EngagementDecision(3, "conversation_no_booking")
    if came_from_paid_advertising(metadata):
        return EngagementDecision(3, "paid_ad_no_meaningful_reply")
    return EngagementDecision(3, "no_meaningful_reply")
