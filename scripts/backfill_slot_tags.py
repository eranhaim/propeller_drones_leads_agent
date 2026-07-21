"""Backfill חלון: <slot> tags in LeadMe for all leads that have a
preferred_call_slot in the DB but never got the tag pushed.

Safe to re-run: LeadMe deduplicates tags internally.

Usage (inside container):
    docker exec -e PYTHONPATH=/app propeller_bot python scripts/backfill_slot_tags.py
    docker exec -e PYTHONPATH=/app propeller_bot python scripts/backfill_slot_tags.py --dry-run
"""
from __future__ import annotations
import sys

from loguru import logger

from app.db.models import Lead, FunnelStage
from app.db.session import session_scope
from app.crm.leadme_delete import _build_client, find_leadme_id_by_phone
from app.crm.leadme_client import _admin_add_tag

DRY_RUN = "--dry-run" in sys.argv


def main() -> None:
    client = _build_client()
    if client is None:
        logger.error("No LeadMe cookies configured — aborting.")
        sys.exit(1)

    with session_scope() as s:
        rows = (
            s.query(Lead.phone, Lead.lead_metadata)
            .filter(Lead.funnel_stage == FunnelStage.handed_off)
            .all()
        )

    ok = skipped = failed = 0
    for phone, lead_metadata in rows:
        slot = (lead_metadata or {}).get("preferred_call_slot")
        if not slot:
            skipped += 1
            continue

        tag = f"חלון: {slot}"
        leadme_id = find_leadme_id_by_phone(phone, client)
        if not leadme_id:
            logger.warning("phone={} — not found in LeadMe, skipping", phone)
            skipped += 1
            continue

        if DRY_RUN:
            logger.info("[DRY-RUN] would add tag={!r} to leadme_id={} phone={}", tag, leadme_id, phone)
            ok += 1
            continue

        result = _admin_add_tag(client, leadme_id, tag)
        if result:
            logger.info("OK  leadme_id={} phone={} tag={!r}", leadme_id, phone, tag)
            ok += 1
        else:
            logger.warning("FAIL leadme_id={} phone={} tag={!r}", leadme_id, phone, tag)
            failed += 1

    client.close()
    logger.info("Done. ok={} skipped={} failed={}", ok, skipped, failed)


if __name__ == "__main__":
    main()
