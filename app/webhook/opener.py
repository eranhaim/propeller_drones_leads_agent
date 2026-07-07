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

from typing import Any, Dict, Optional

from loguru import logger
from whatsapp_api_client_python.API import GreenAPI

from app.config import get_settings
from app.db import repository
from app.db.models import MessageRole
from app.db.session import session_scope


OPENER_TEMPLATE_KNOWN_NAME = (
    "היי {name} 🙋\n"
    "אני אלעד מהאקדמיה של פרופלור דרונס - החברה המובילה בישראל לרחפנים "
    "מסחריים והכשרות טייסי רחפנים.\n\n"
    "השארת פרטים אצלנו לגבי {topic}. אני כאן להסביר לך כל מה שרוצה לדעת "
    "ולעזור לך להבין אם זה מתאים לך.\n\n"
    "בשביל להתקדם - איזה סוג ניסיון יש לך היום עם רחפנים? "
    "(סתם תחביב, כבר עסקתי בזה, או שאני מתחיל לגמרי מאפס)"
)

OPENER_TEMPLATE_ANON = (
    "היי 🙋\n"
    "אני אלעד מהאקדמיה של פרופלור דרונס - החברה המובילה בישראל לרחפנים "
    "מסחריים והכשרות טייסי רחפנים.\n\n"
    "השארת פרטים אצלנו לגבי {topic} ואני כאן להסביר לך כל מה שרוצה לדעת "
    "ולעזור לך להבין אם זה מתאים לך.\n\n"
    "בשביל להתקדם - איך קוראים לך, ואיזה ניסיון יש לך היום עם רחפנים?"
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
    clean_name = (name or "").strip().split(" ", 1)[0]  # first word only
    if clean_name and not clean_name.isdigit():
        return OPENER_TEMPLATE_KNOWN_NAME.format(name=clean_name, topic=topic)
    return OPENER_TEMPLATE_ANON.format(topic=topic)


def handle_new_lead(
    phone: str,
    name: Optional[str],
    metadata: Dict[str, Any],
    campaign_id: Optional[str],
) -> None:
    """Upsert + opener. Safe to run on a daemon thread (own DB session)."""
    try:
        with session_scope() as session:
            lead = repository.get_or_create_lead(
                session, phone=phone, name=name,
            )
            existing_meta = dict(lead.lead_metadata or {})
            already_contacted = bool(existing_meta.get("opener_sent_at"))
            history = repository.recent_messages(session, lead, limit=1)

            repository.update_lead_metadata(session, lead, **metadata)

            if already_contacted or history:
                logger.info(
                    "[opener] lead {} already engaged (opener_sent_at={}, "
                    "history_len={}), skipping opener.",
                    phone, existing_meta.get("opener_sent_at"), len(history),
                )
                return

            topic = _pick_topic(campaign_id, {**existing_meta, **metadata})
            text = _render_opener(name, topic)

            try:
                api = _greenapi_client()
                api.sending.sendMessage(_chat_id(phone), text)
            except Exception:
                logger.exception("[opener] failed to send WhatsApp to {}", phone)
                return

            repository.add_message(session, lead, MessageRole.assistant, text)
            from datetime import datetime, timezone
            repository.update_lead_metadata(
                session, lead,
                opener_sent_at=datetime.now(timezone.utc).isoformat(),
                opener_campaign_id=campaign_id or "",
            )
            logger.info("[opener] sent to {} (campaign={}, topic={!r})",
                        phone, campaign_id, topic)
    except Exception:
        logger.exception("[opener] unexpected error handling {}", phone)
