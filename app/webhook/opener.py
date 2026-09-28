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
from whatsapp_api_client_python.API import GreenAPI

from app.config import get_settings
from app.db import repository
from app.db.models import Lead, MessageRole
from app.db.session import session_scope
from app.names import first_name


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


def _normalize_source(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _is_level_one_source(source: object) -> bool:
    """Return whether LeadMe supplied one of Roy's priority sources."""
    normalized = _normalize_source(source)
    if not normalized:
        return False
    configured = {
        _normalize_source(item)
        for item in get_settings().leadme_level_1_sources
    }
    return normalized in configured


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


def handle_new_lead(
    phone: str,
    name: Optional[str],
    metadata: Dict[str, Any],
    campaign_id: Optional[str],
) -> None:
    """Record a LeadMe lead and classify its initial CRM priority.

    A LeadMe webhook does not prove WhatsApp consent. Sending an opener is
    therefore opt-in; the default records the lead and CRM state only.
    """
    try:
        source = metadata.get("leadme_source")
        is_level_one_source = _is_level_one_source(source)
        should_send_opener = False

        with session_scope() as session:
            lead = repository.get_or_create_lead(
                session, phone=phone, name=name,
            )
            existing_meta = dict(lead.lead_metadata or {})
            already_contacted = bool(existing_meta.get("opener_sent_at"))
            history = repository.recent_messages(session, lead, limit=1)

            repository.update_lead_metadata(
                session,
                lead,
                **metadata,
                leadme_webhook_received_at=datetime.now(timezone.utc).isoformat(),
                leadme_source_is_level_1=is_level_one_source,
            )
            should_send_opener = (
                get_settings().webhook_opener_enabled
                and not already_contacted
                and not history
            )

            if not should_send_opener:
                logger.info(
                    "[opener] WhatsApp opener disabled or lead already engaged "
                    "(phone={}, enabled={}, opener_sent_at={}, history_len={})",
                    phone,
                    get_settings().webhook_opener_enabled,
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
                    logger.exception("[opener] failed to send WhatsApp to {}", phone)
                else:
                    repository.add_message(session, lead, MessageRole.assistant, text)
                    repository.update_lead_metadata(
                        session, lead,
                        opener_sent_at=datetime.now(timezone.utc).isoformat(),
                        opener_campaign_id=campaign_id or "",
                    )
                    logger.info(
                        "[opener] sent to {} (campaign={}, topic={!r})",
                        phone, campaign_id, topic,
                    )

        # Roy's mapping: only website home, incoming-call and landing-page
        # sources are L1 before a booking. Every other new webhook lead is L3
        # until a meaningful reply upgrades it to L2.
        try:
            from app.crm.client import mark_no_reply, mark_ready_for_call
            with session_scope() as s3:
                l3 = s3.query(Lead).filter_by(phone=phone).first()
                if l3 is not None:
                    if is_level_one_source:
                        mark_ready_for_call(
                            l3,
                            note=f"priority source={source}",
                            session=s3,
                        )
                    else:
                        mark_no_reply(
                            l3,
                            note="new LeadMe webhook lead; no bot reply yet",
                        )
        except Exception:
            logger.exception("[opener] initial LeadMe level push failed for {}", phone)
    except Exception:
        logger.exception("[opener] unexpected error handling {}", phone)
