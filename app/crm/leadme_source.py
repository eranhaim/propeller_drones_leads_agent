"""Pull each lead's real source out of LeadMe, instead of waiting for it.

Why this exists
---------------
LeadMe's external interface fires at record intake, before the source, the
campaign, the status and the tags are written, so the webhook payload almost
never carries any of them. The finished record only ever exists in LeadMe. So
the bot fetches it rather than depending on a push that is structurally too
early (Eran, 2026-10-13).

What is reachable
-----------------
v3 has no per-lead endpoint for the source field -- ``getLead``,
``getLeadDetails``, ``getLeadInfo`` and their obvious variants all 404
(probed 2026-10-13). What it does expose is the lead's TAGS, by phone, and
that is where the human-meaningful source lives anyway:

    שיחה נכנסת · אתר הבית · עמוד נחיתה אקדמיה מובייל · וובינר ·
    קמפיין facebook: … · Whatsapp

:func:`app.crm.levels.level_1_source_reason` reads those tags.

Where it runs
-------------
On the scheduler, never on the reply path. A lead's level is recomputed on
every turn, so a tag that lands between two messages is picked up on the next
one; blocking a reply on an HTTP round-trip to buy a few minutes would be a
bad trade, and the same rule already governs every other CRM call.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select

from app.crm.leadme_v3 import get_lead_tags_by_phone, is_v3_available
from app.db import repository
from app.db.models import Lead
from app.db.session import session_scope

# How long to keep looking for a lead LeadMe does not have yet. Covers the
# CTWA race -- the ad reaches WhatsApp before the supplier sync reaches
# LeadMe -- with room to spare. Past this a missing lead is a data problem,
# not a timing one, and re-asking daily forever buys nothing.
RETRY_MISSING_FOR_DAYS = 7

# Gap between attempts for a lead LeadMe does not have yet.
RETRY_INTERVAL_HOURS = 6

# Leads refreshed per tick. Each costs two v3 calls and LeadMe allows 60 a
# minute across the whole process, so this leaves room for the push queue
# and for any level the live conversation needs to write.
BATCH_SIZE = 20


def _as_utc(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def needs_refresh(lead: Lead, now: datetime) -> bool:
    """Return whether this lead's source is worth asking LeadMe about.

    Once we have the tags we stop: they describe where the lead came from,
    which cannot change afterwards.
    """
    metadata = lead.lead_metadata or {}
    if metadata.get("leadme_tags"):
        return False

    last_attempt = _as_utc(metadata.get("leadme_source_fetched_at"))
    if last_attempt is None:
        return True
    if now - last_attempt < timedelta(hours=RETRY_INTERVAL_HOURS):
        return False
    created_at = _as_utc(lead.created_at)
    if created_at is None:
        return True
    return now - created_at < timedelta(days=RETRY_MISSING_FOR_DAYS)


def refresh_lead_source(session, lead: Lead, now: datetime) -> str:
    """Fetch one lead's tags from LeadMe and merge them into its metadata.

    Returns the lookup outcome. An ``unavailable`` answer records nothing:
    backing off six hours because LeadMe was busy for a second would be the
    rate limit quietly turning into missing data.
    """
    outcome, tags = get_lead_tags_by_phone(lead.phone or "")
    if outcome == "unavailable":
        return outcome

    fields = {"leadme_source_fetched_at": now.isoformat()}
    if tags:
        fields["leadme_tags"] = tags
    repository.update_lead_metadata(session, lead, **fields)
    return outcome


def refresh_missing_sources() -> None:
    """Scheduler entry point. Fill in the source for leads still missing one.

    Opens one session per lead so a slow or locked row cannot stall the
    batch, and swallows every per-lead failure: this job is an enrichment,
    and nothing downstream may break because LeadMe was unreachable.
    """
    if not is_v3_available():
        logger.info("[leadme-source] tick skipped: v3 is not available")
        return

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        due_ids = [
            lead.id
            for lead in session.execute(select(Lead)).scalars().all()
            if needs_refresh(lead, now)
        ]
    if not due_ids:
        logger.info("[leadme-source] tick: every lead already has a source")
        return

    outcomes = Counter()
    for lead_id in due_ids[:BATCH_SIZE]:
        try:
            with session_scope() as session:
                lead = session.get(Lead, lead_id)
                if lead is None:
                    continue
                outcomes[refresh_lead_source(session, lead, now)] += 1
        except Exception:  # noqa: BLE001
            logger.exception("[leadme-source] refresh raised for lead {}", lead_id)
        if outcomes["unavailable"]:
            # Out of rate budget. Stop rather than burn the tick on calls
            # LeadMe will not answer; the next tick resumes where this left off.
            break

    logger.info(
        "[leadme-source] tick: {} due, found {}, absent {}, stopped on rate "
        "limit after {} lead(s)",
        len(due_ids),
        outcomes["found"],
        outcomes["absent"],
        sum(outcomes.values()),
    )
