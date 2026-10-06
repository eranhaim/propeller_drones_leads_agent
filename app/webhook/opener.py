"""What we do when a fresh lead lands from LeadMe.

1. Upsert the ``Lead`` row (dedupe on phone -- if the user already talked
   to us we do NOT re-send an opener).
2. Save the LeadMe-provided metadata (campaign, custom-field answers,
   original comment).
3. Send a warm, personalized opener over WhatsApp so the user is engaged
   before the sales team ever picks up the phone.

The opener is intentionally NOT run through the LLM agent: on the very
first contact we know almost nothing about the user beyond what LeadMe
told us, and using a canned Hebrew template keeps latency low and the
first impression consistent. Once the user replies, the standard message
handler takes over and the full LangChain agent runs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import select
from whatsapp_api_client_python.API import GreenAPI

from app.agent.context import VideoSend
from app.agent.memory import record_assistant_turn
from app.agent.tools import record_video_delivery
from app.config import get_settings
from app.db import repository
from app.db.models import Lead, MessageRole
from app.db.session import session_scope
from app.names import first_name
from app.videos.catalog import get_video
from app.whatsapp.sender import ChatSender


OPENER_TEMPLATE_KNOWN_NAME = (
    "היי {name} 🙋\n"
    "אני אלעד מהאקדמיה של פרופלור דרונס - החברה המובילה בישראל לשירותי רחפנים "
    "מסחריים והכשרת מטיסי רחפנים.\n\n"
    "השארת פרטים אצלנו לגבי {topic}. אני כאן להסביר לך כל מה שרוצה לדעת "
    "ולעזור לך להבין אם זה מתאים לך.\n\n"
    "בשביל להתקדם - ספר לי במה אתה מתעניין — לימוד התחום, שירותי רחפן, או רכישת רחפן?"
)

OPENER_TEMPLATE_ANON = (
    "היי 🙋\n"
    "אני אלעד מהאקדמיה של פרופלור דרונס - החברה המובילה בישראל לשירותי רחפנים "
    "מסחריים והכשרת מטיסי רחפנים.\n\n"
    "השארת פרטים אצלנו לגבי {topic} ואני כאן להסביר לך כל מה שרוצה לדעת "
    "ולעזור לך להבין אם זה מתאים לך.\n\n"
    "בשביל להתקדם - איך קוראים לך, ובמה אתה מתעניין — לימוד התחום, שירותי רחפן, או רכישת רחפן?"
)

# Campaign_id -> friendly Hebrew topic word for the opener line.
CAMPAIGN_TOPIC = {
    "12277": "קורס הטסת רחפנים",       # WhatsApp leads
    "12293": "קורס הטסת רחפנים",       # organic leads
    "12284": "אקדמיית הרחפנים",        # academy trial
    "12292": "אקדמיית הרחפנים",        # academy alumni
    "13829": "רחפן חדש לקנייה",        # sales
    "12719": "שירות רחפנים לצילום מקצועי",  # services
    "12424": "קורס הטסת רחפנים",
    "12425": "קורס הטסת רחפנים",
}
DEFAULT_TOPIC = "עולם הרחפנים"

# Sent right after the opener text so a first-contact lead sees who we are
# before anything else. Marked as delivered so the agent never offers it
# again as if it were new.
OPENER_VIDEO_ID = "propeller_company_overview"


def _normalize_source(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _is_website_form_source(source: object) -> bool:
    """Return whether LeadMe supplied an approved website-form source."""
    normalized = _normalize_source(source)
    if not normalized:
        return False
    configured = {
        _normalize_source(item)
        for item in get_settings().leadme_website_form_sources
    }
    return normalized in configured


def _is_website_form_campaign(campaign_id: object) -> bool:
    """Return whether a configured LeadMe campaign is the website form."""
    normalized = _normalize_source(campaign_id)
    if not normalized:
        return False
    configured = {
        _normalize_source(item)
        for item in get_settings().leadme_website_form_campaigns
    }
    return normalized in configured


def _is_website_form_lead(source: object, campaign_id: object) -> bool:
    """Identify a website form from its source, or its configured campaign."""
    return (
        _is_website_form_source(source)
        or _is_website_form_campaign(campaign_id)
    )


def _initial_priority(source: object, campaign_id: object) -> tuple[int, str]:
    """Classify a fresh webhook lead into an initial engagement level.

    A new approved website form starts hot (L1). Its later WhatsApp activity
    is classified dynamically by ``classify_engagement`` and can downgrade it
    to L2 when the follow-up is weak. Other sources start at L3 until they
    provide meaningful evidence.
    """
    if _is_website_form_lead(source, campaign_id):
        return 1, "website_initial"
    return 3, "unengaged"


def _should_send_website_form_opener(
    source: object,
    campaign_id: object,
    metadata: Dict[str, Any],
    history: list[object],
    is_new_lead: bool,
) -> bool:
    """Allow one immediate opener for a new, approved website-form lead."""
    return (
        get_settings().website_form_opener_enabled
        and _is_website_form_lead(source, campaign_id)
        and is_new_lead
        and not metadata.get("opener_sent_at")
        and not history
    )


def _greenapi_client() -> GreenAPI:
    settings = get_settings()
    return GreenAPI(
        settings.green_api_instance_id,
        settings.green_api_token,
    )


def _chat_id(phone: str) -> str:
    return f"{phone}@c.us"


def _pick_topic(campaign_id: Optional[str], metadata: Dict[str, Any]) -> str:
    if campaign_id and campaign_id in CAMPAIGN_TOPIC:
        return CAMPAIGN_TOPIC[campaign_id]
    # Users often typed a course name in the comment field on LeadMe.
    comment = (metadata.get("leadme_raw_comment") or "").strip()
    if comment and len(comment) < 60:
        return comment
    return DEFAULT_TOPIC


def _render_opener(name: Optional[str], topic: str) -> str:
    clean_name = first_name(name)
    if clean_name:
        return OPENER_TEMPLATE_KNOWN_NAME.format(name=clean_name, topic=topic)
    return OPENER_TEMPLATE_ANON.format(topic=topic)


def _send_opener_video(session, lead: Lead, api: GreenAPI) -> None:
    """Deliver the company intro video after the opener text.

    Called only once the opener text is already sent and recorded, and every
    failure is swallowed: the welcome message is the thing that must never be
    lost, and a missing video is recoverable while a missing greeting is not.
    """
    if OPENER_VIDEO_ID in (lead.videos_sent or []):
        return

    video = get_video(OPENER_VIDEO_ID)
    if video is None:
        logger.error(
            "[opener] intro video {!r} missing from the catalog",
            OPENER_VIDEO_ID,
        )
        return

    caption = video.caption or video.title
    try:
        ChatSender(api=api, chat_id=_chat_id(lead.phone)).send_video(video, caption)
    except Exception:
        logger.exception(
            "[opener] intro video send failed for lead_id={}; opener text "
            "was already delivered",
            lead.id,
        )
        return

    record_video_delivery(
        session,
        lead,
        video,
        VideoSend(video_id=video.id, title=video.title, caption=caption),
    )
    logger.info("[opener] intro video sent for lead_id={}", lead.id)


def handle_new_lead(
    phone: str,
    name: Optional[str],
    metadata: Dict[str, Any],
    campaign_id: Optional[str],
) -> None:
    """Record a LeadMe lead and classify its initial CRM priority.

    Only approved website-form sources receive one first-contact opener.
    Other webhook sources remain CRM-only under Roy's no-follow-up policy.
    """
    try:
        source = metadata.get("leadme_source")
        is_website_form_source = _is_website_form_lead(source, campaign_id)
        initial_priority, priority_reason = _initial_priority(source, campaign_id)
        is_level_one_source = initial_priority == 1
        should_send_opener = False
        lead_id: Optional[int] = None
        duplicate_facebook_lead = False

        with session_scope() as session:
            existing_lead = session.execute(
                select(Lead.id)
                .where(Lead.phone == phone)
                .with_for_update()
            ).scalar_one_or_none()
            facebook_lead_id = str(
                metadata.get("leadme_facebook_lead_id") or ""
            ).strip()
            matching_facebook_lead = repository.get_lead_by_facebook_lead_id(
                session, facebook_lead_id,
            )
            if (
                matching_facebook_lead is not None
                and (
                    existing_lead is None
                    or matching_facebook_lead.id != existing_lead
                )
            ):
                logger.warning(
                    "[opener] ignored Facebook Lead ID collision "
                    "(incoming_phone={}, existing_lead_id={})",
                    phone,
                    matching_facebook_lead.id,
                )
                return

            lead = repository.get_or_create_lead(
                session, phone=phone, name=name,
            )
            lead_id = lead.id
            duplicate_facebook_lead = bool(
                facebook_lead_id
                and matching_facebook_lead is not None
                and matching_facebook_lead.id == lead.id
            )
            # Hold the lead row lock through the send and persisted marker so
            # a duplicate webhook cannot send a second opener.
            session.refresh(lead, with_for_update=True)
            existing_meta = dict(lead.lead_metadata or {})
            history = repository.recent_messages(session, lead, limit=1)

            is_reentry = existing_lead is not None and not duplicate_facebook_lead
            if is_reentry:
                existing_meta.pop("not_relevant", None)
                existing_meta.pop("leadme_relevance", None)
                lead.lead_metadata = existing_meta
                session.flush()

            repository.update_lead_metadata(
                session,
                lead,
                **metadata,
                leadme_webhook_received_at=datetime.now(timezone.utc).isoformat(),
                leadme_source_is_level_1=is_level_one_source,
                leadme_source_is_website_form=is_website_form_source,
                leadme_initial_priority=initial_priority,
                leadme_priority_reason=priority_reason,
                leadme_reentry_at=(
                    datetime.now(timezone.utc).isoformat() if is_reentry else None
                ),
            )
            should_send_opener = _should_send_website_form_opener(
                source, campaign_id,
                existing_meta,
                history,
                is_new_lead=existing_lead is None and not duplicate_facebook_lead,
            )

            if not should_send_opener:
                logger.info(
                    "[opener] skipping WhatsApp opener "
                    "(lead_id={}, website_form_source={}, enabled={}, "
                    "is_new={}, duplicate_event={}, opener_sent_at={}, history_len={})",
                    lead.id,
                    is_website_form_source,
                    get_settings().website_form_opener_enabled,
                    existing_lead is None,
                    duplicate_facebook_lead,
                    existing_meta.get("opener_sent_at"),
                    len(history),
                )
            else:
                topic = _pick_topic(campaign_id, {**existing_meta, **metadata})
                text = _render_opener(name, topic)
                try:
                    api = _greenapi_client()
                    api.sending.sendMessage(_chat_id(phone), text)
                except Exception:
                    logger.exception("[opener] failed to send WhatsApp for lead_id={}", lead.id)
                else:
                    repository.add_message(session, lead, MessageRole.assistant, text)
                    record_assistant_turn(session, lead, text)
                    repository.update_lead_metadata(
                        session, lead,
                        opener_sent_at=datetime.now(timezone.utc).isoformat(),
                        opener_campaign_id=campaign_id or "",
                    )
                    logger.info(
                        "[opener] sent for lead_id={} (campaign={}, topic={!r})",
                        lead.id, campaign_id, topic,
                    )
                    # Text first, then media: the lead should see who is
                    # writing before a video lands in the chat.
                    _send_opener_video(session, lead, api)

        if duplicate_facebook_lead:
            logger.info(
                "[opener] ignored duplicate Facebook Lead event for lead_id={}",
                lead_id,
            )
            return

        # A repeat form/call is new activity on an existing lead. It is L2
        # unless a later conversation independently satisfies an L1 rule.
        try:
            from app.crm.client import mark_engaged_no_book, mark_no_reply, mark_ready_for_call
            with session_scope() as s3:
                l3 = s3.query(Lead).filter_by(phone=phone).first()
                if l3 is not None:
                    if is_reentry:
                        mark_engaged_no_book(
                            l3,
                            note="existing lead submitted new activity",
                            session=s3,
                        )
                    elif initial_priority == 1:
                        mark_ready_for_call(
                            l3,
                            note=f"LeadMe webhook priority={priority_reason}",
                            session=s3,
                        )
                    else:
                        mark_no_reply(
                            l3,
                            note="new LeadMe webhook lead; no bot reply yet",
                            session=s3,
                        )
        except Exception:
            logger.exception(
                "[opener] initial LeadMe level push failed for lead_id={}",
                lead_id,
            )
    except Exception:
        logger.exception("[opener] unexpected error handling LeadMe webhook")
        raise
