"""Periodic follow-up nudges for silent leads.

Problem: a lead lands, the bot has a few messages, then the lead just
stops replying. Without a nudge they go cold. This module runs a
background job every ``FOLLOWUP_INTERVAL_MINUTES`` that finds leads
matching all of the following:

- ``funnel_stage`` is NOT ``handed_off`` (we already handed them off to
  sales -- silence is fine; the sales rep is on it).
- The most recent DB message for the lead was sent BY US (assistant).
  If the last message is theirs, we're the ones who owe them a reply --
  the agent will handle it when their next inbound arrives.
- ``last_message_at`` is older than the nudge threshold for the current
  nudge number (24h -> 72h defaults).
- We haven't already sent ``FOLLOWUP_MAX_NUDGES`` nudges (default 2).
- Current Israel-local time is within the "polite" window (default
  09:00-20:00). We do NOT wake people up at 3am.

Each nudge is a canned Hebrew template so we don't burn LLM tokens or
risk the model hallucinating something incoherent from a stale context.
The nudge is recorded in ``messages`` as ``role=assistant`` with
``msg_metadata={"nudge": N}`` so the agent's context on the next inbound
naturally includes it (the LLM will see "I sent them a follow-up 3 days
ago" and behave sensibly).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select
from whatsapp_api_client_python.API import GreenAPI

from app.config import get_settings
from app.db import repository
from app.db.models import FunnelStage, Lead, Message, MessageRole
from app.db.session import session_scope

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


# --- nudge copy ---------------------------------------------------------
# Kept as canned Hebrew templates: safe, cheap, and predictable. If we
# ever want LLM-generated nudges (referring to the last thing the lead
# said), swap _render_nudge for a call to the agent.

_NUDGE_TEMPLATES_KNOWN = {
    1: (
        "היי {name} 👋\n"
        "רק בודק שלא נעלמת עליי - נשארה לי הרגשה שהיה משהו שכן היה מעניין אותך "
        "מעולם הרחפנים. יש עוד משהו שרצית שאסביר לך? "
        "או שאפשר לקפוץ צעד קדימה ולתאם שיחה קצרה עם נציג שיסביר את המסלולים בדיוק?"
    ),
    2: (
        "היי {name} 🙌\n"
        "בטח היה עמוס. רק להשאיר את זה פתוח: "
        "אפשר לתאם עכשיו שיחה של 10 דקות עם נציג - בלי התחייבות, פשוט נסביר "
        "לך את המסלולים ותקבל תמונה מלאה. באיזה חלון שעות עדיף לך - 9-12, 12-15, "
        "או 15-18?"
    ),
}

_NUDGE_TEMPLATES_ANON = {
    1: (
        "היי 👋\n"
        "רק בודק שלא נעלמת עליי - היה משהו שרצית לשמוע עליו מעולם הרחפנים? "
        "או שאפשר לתאם שיחה קצרה עם נציג שיסביר לך את המסלולים?"
    ),
    2: (
        "היי 🙌\n"
        "בטח היה עמוס. אם רלוונטי, אפשר לתאם עכשיו שיחה של 10 דקות עם נציג - "
        "בלי התחייבות. באיזה חלון שעות עדיף לך: 9-12, 12-15, או 15-18?"
    ),
}


def _render_nudge(name: Optional[str], nudge_number: int) -> Optional[str]:
    first = ((name or "").strip().split(" ", 1)[0] or "").strip()
    if first and not first.isdigit():
        tpl = _NUDGE_TEMPLATES_KNOWN.get(nudge_number)
        return tpl.format(name=first) if tpl else None
    tpl = _NUDGE_TEMPLATES_ANON.get(nudge_number)
    return tpl or None


# --- selection ----------------------------------------------------------


def _is_within_quiet_hours() -> bool:
    """Return True if we're OUTSIDE the polite window (i.e. do NOT send)."""
    settings = get_settings()
    now = datetime.now(ISRAEL_TZ)
    if now.hour < settings.followup_quiet_start_hour:
        return True
    if now.hour >= settings.followup_quiet_end_hour:
        return True
    return False


def _pick_leads_to_nudge(session, now: datetime) -> list[Lead]:
    settings = get_settings()
    first_after = now - timedelta(hours=settings.followup_first_hours)
    max_nudges = settings.followup_max_nudges

    candidates = list(session.execute(
        select(Lead).where(
            Lead.funnel_stage != FunnelStage.handed_off,
            Lead.last_message_at != None,  # noqa: E711
            Lead.last_message_at < first_after,
        )
    ).scalars().all())

    picks: list[Lead] = []
    for lead in candidates:
        md = dict(lead.lead_metadata or {})
        nudges_sent = int(md.get("nudge_count", 0) or 0)
        if nudges_sent >= max_nudges:
            continue

        # Only nudge if the most recent message was OURS (we're waiting on them).
        last_msg = session.execute(
            select(Message)
            .where(Message.lead_id == lead.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last_msg is None or last_msg.role != MessageRole.assistant:
            continue

        # For nudge #2 we require ANOTHER FOLLOWUP_SECOND_HOURS to have passed
        # since the last nudge (not since original silence started).
        if nudges_sent >= 1:
            last_nudge_iso = md.get("last_nudge_at")
            if last_nudge_iso:
                try:
                    last_nudge_at = datetime.fromisoformat(last_nudge_iso)
                except ValueError:
                    last_nudge_at = None
                if last_nudge_at:
                    if now - last_nudge_at < timedelta(
                        hours=settings.followup_second_hours
                    ):
                        continue

        picks.append(lead)
    return picks


# --- send ---------------------------------------------------------------


def _greenapi_client() -> GreenAPI:
    settings = get_settings()
    return GreenAPI(settings.green_api_instance_id, settings.green_api_token)


def _send_and_record(lead: Lead, nudge_number: int) -> bool:
    text = _render_nudge(lead.name, nudge_number)
    if not text:
        logger.warning(
            "[followup] no template for nudge#{} lead={}, skipping",
            nudge_number, lead.id,
        )
        return False

    api = _greenapi_client()
    chat_id = f"{lead.phone}@c.us"
    try:
        api.sending.sendMessage(chat_id, text)
    except Exception:
        logger.exception("[followup] send failed for lead {}", lead.id)
        return False

    return True


def run_once() -> None:
    """One pass: pick eligible leads and send nudges. Safe to call from
    a scheduler tick or a manual admin command."""
    if _is_within_quiet_hours():
        logger.debug("[followup] within quiet hours, skipping tick")
        return

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        leads = _pick_leads_to_nudge(session, now)
        if not leads:
            logger.debug("[followup] no leads eligible for nudging")
            return

        logger.info("[followup] {} lead(s) eligible for nudging", len(leads))
        for lead in leads:
            md = dict(lead.lead_metadata or {})
            nudge_number = int(md.get("nudge_count", 0) or 0) + 1

            ok = _send_and_record(lead, nudge_number)
            if not ok:
                continue

            text = _render_nudge(lead.name, nudge_number) or ""
            msg = repository.add_message(
                session, lead, MessageRole.assistant, text,
                metadata={"nudge": nudge_number},
            )
            repository.update_lead_metadata(
                session, lead,
                nudge_count=nudge_number,
                last_nudge_at=now.isoformat(),
            )
            logger.info(
                "[followup] nudged lead {} (nudge#{}, msg={})",
                lead.id, nudge_number, msg.id,
            )


# --- scheduler wiring ---------------------------------------------------


def run_in_background_thread() -> None:
    """Start APScheduler in a daemon thread. Cheap: one Python thread,
    no new container."""
    settings = get_settings()
    if not settings.followup_enabled:
        logger.info("[followup] disabled via FOLLOWUP_ENABLED=false")
        return

    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone=ISRAEL_TZ)
    scheduler.add_job(
        run_once,
        trigger="interval",
        minutes=settings.followup_interval_minutes,
        id="followup_tick",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(ISRAEL_TZ) + timedelta(minutes=1),
    )

    def _start() -> None:
        try:
            scheduler.start()
            logger.info(
                "[followup] scheduler started (every {}min, first={}h, "
                "second={}h, max={} nudges, active {:02d}:00-{:02d}:00 Asia/Jerusalem)",
                settings.followup_interval_minutes,
                settings.followup_first_hours,
                settings.followup_second_hours,
                settings.followup_max_nudges,
                settings.followup_quiet_start_hour,
                settings.followup_quiet_end_hour,
            )
        except Exception:
            logger.exception("[followup] scheduler failed to start")

    Thread(target=_start, daemon=True, name="followup-scheduler").start()
