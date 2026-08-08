"""Persistent retry queue for LeadMe admin pushes.

Motivation -- the CTWA race
---------------------------
Facebook / TikTok / other CTWA (Click-To-WhatsApp) leads reach our bot
FASTER than the customer's own LeadMe supplier sync updates LeadMe's
DB. So when the bot tries to change the lead's status or add a tag in
LeadMe, the phone often isn't findable yet. Historical behaviour was:
try 4 times over 30 seconds, then give up FOREVER. Result: 40 of every
44 pushes silently dropped, sales team sees leads stuck at "חדש", and
the customer thinks "the LeadMe integration is broken".

The fix here is a durable, JSON-backed retry queue stored in
``lead.lead_metadata['leadme_push_pending']``. When ``push_lead`` (or
the CTWA tag path) can't resolve the phone within the fast in-request
attempts, it queues the desired action in the lead's metadata blob.
A scheduler tick then retries every few minutes, up to ~1 hour total,
before giving up loudly and marking the lead ``leadme_push_abandoned``
so an operator can act.

State schema (on ``lead.lead_metadata``)
----------------------------------------
::

    {
        "leadme_push_pending": [
            {"kind": "engagement", "level": 1, "slot": "9-12",
             "note": "intent=course|slot=9-12"},
            {"kind": "ctwa_tag", "campaign": "עודד"},
        ],
        "leadme_push_next_attempt_at": "2026-07-26T10:25:00+00:00",
        "leadme_push_attempts": 3,
        "leadme_push_queued_at": "2026-07-26T10:12:00+00:00",
        # Only set once we give up:
        "leadme_push_abandoned": True,
        "leadme_push_abandoned_at": "2026-07-26T11:12:00+00:00",
    }

Kinds are additive: if the bot enqueues an engagement push and later
enqueues a CTWA tag for the same lead (or vice versa), both live on
the same pending list and get replayed together (single client, single
row lookup, cheap).

Idempotency and level upgrades
------------------------------
- Two ``engagement`` pushes for the same lead collapse into one -- the
  LOWER level wins (level 1 = booked beats level 2 = replied beats
  level 3 = silent). Slot from either is preserved (newer wins). Note
  strings are concatenated so operators can see the full trail.
- Two ``ctwa_tag`` pushes with the same campaign collapse into one.
  Different campaigns keep both entries -- unlikely in practice but
  cheap to preserve.
- Once a pending item is successfully executed, it's removed. When the
  list is empty, all queue metadata is cleared so the lead looks clean
  again.

Why not a dedicated table?
--------------------------
Because ``lead_metadata`` is already JSON, already flushed on every
lead update, already visible in the admin UI, and already backed up by
the same Postgres. A dedicated table would need a migration, its own
admin visibility, and a foreign-key story. The queue is small (<1
record per pending lead), so JSON is the pragmatic choice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Lead
from app.db.session import session_scope


# --- Tunables -----------------------------------------------------------

# Total lifetime of a pending item, in minutes. After this we give up
# and log a loud ERROR. Chosen empirically: Facebook's supplier-sync
# to LeadMe typically lands within 15 minutes; the tail of the
# distribution can push to ~45 minutes. 60 minutes covers ~99% of
# real cases; anything longer is very likely a data problem (the lead
# is in a campaign the bot doesn't have visibility into, or the phone
# doesn't match). See :func:`retry_pending_pushes`.
MAX_LIFETIME_MINUTES = 180

# Interval between retry attempts, in minutes. Linear -- no fancy
# exponential backoff. LeadMe rate-limits us implicitly via CSRF/session
# freshness, so hammering more often than every 3 minutes buys nothing.
RETRY_INTERVAL_MINUTES = 3

# Cap on attempts (defense-in-depth alongside MAX_LIFETIME_MINUTES).
# MAX_LIFETIME_MINUTES / RETRY_INTERVAL_MINUTES rounded up = 60.
MAX_ATTEMPTS = 60


# --- Queue mutations ----------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_attempt_iso(now: Optional[datetime] = None) -> str:
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(minutes=RETRY_INTERVAL_MINUTES)).isoformat()


def _merge_engagement(existing: list[dict], new_item: dict) -> list[dict]:
    """Fold a new engagement item into an existing pending list.

    Rule: if there's already an engagement entry, we KEEP the one with
    the lower level (== more engaged). Slot from the newer wins if the
    older didn't have one. Notes get concatenated with a separator so
    the sales team can see the full trail on the eventual LeadMe push.
    """
    kept: list[dict] = []
    merged_engagement: Optional[dict] = None
    for item in existing:
        if item.get("kind") != "engagement":
            kept.append(item)
            continue
        if merged_engagement is None:
            merged_engagement = dict(item)
        else:
            # Shouldn't normally happen (we always merge on write) but
            # be defensive against migrated data.
            merged_engagement = _pick_better_engagement(merged_engagement, item)

    if merged_engagement is None:
        kept.append(new_item)
    else:
        kept.append(_pick_better_engagement(merged_engagement, new_item))
    return kept


def _pick_better_engagement(a: dict, b: dict) -> dict:
    """Return the "better" of two engagement dicts (lower level wins)."""
    la = int(a.get("level") or 99)
    lb = int(b.get("level") or 99)
    winner = a if la <= lb else b
    loser = b if la <= lb else a
    merged = dict(winner)
    # Slot: prefer whichever slot we have; the winner's slot takes
    # precedence, but if it's empty and loser has one, promote it.
    if not merged.get("slot") and loser.get("slot"):
        merged["slot"] = loser["slot"]
    # Notes: concatenate distinct notes so full trail survives.
    parts: list[str] = []
    for src in (winner, loser):
        n = (src.get("note") or "").strip()
        if n and n not in parts:
            parts.append(n)
    if parts:
        merged["note"] = " || ".join(parts)
    return merged


def _merge_ctwa_tag(existing: list[dict], new_item: dict) -> list[dict]:
    """Fold a new ctwa_tag item; dedupe on (kind, campaign)."""
    new_campaign = (new_item.get("campaign") or "").strip()
    kept: list[dict] = []
    seen = False
    for item in existing:
        if (item.get("kind") == "ctwa_tag"
                and (item.get("campaign") or "").strip() == new_campaign):
            seen = True
            kept.append(item)
            continue
        kept.append(item)
    if not seen:
        kept.append(new_item)
    return kept


def enqueue_engagement(
    lead: Lead,
    level: int,
    slot: Optional[str] = None,
    note: Optional[str] = None,
    *,
    session: Optional[Session] = None,
) -> None:
    """Queue an engagement (status + slot tag) push for later retry.

    Safe to call inside an existing session (pass it in) or standalone
    (we'll open one). The ``lead`` argument must be attached to the
    passed-in session, or -- when ``session`` is None -- we'll re-fetch
    it by id in a fresh scope so we don't accidentally mutate a
    detached instance.
    """
    item = {"kind": "engagement", "level": int(level)}
    if slot:
        item["slot"] = str(slot)
    if note:
        item["note"] = str(note)
    _enqueue(lead, item, merge_fn=_merge_engagement, session=session)


def enqueue_ctwa_tag(
    lead: Lead,
    campaign: str,
    *,
    session: Optional[Session] = None,
) -> None:
    """Queue a ``מקור: <campaign>`` CTWA tag push for later retry."""
    campaign = (campaign or "").strip()
    if not campaign:
        return
    item = {"kind": "ctwa_tag", "campaign": campaign}
    _enqueue(lead, item, merge_fn=_merge_ctwa_tag, session=session)


def _enqueue(lead: Lead, item: dict, merge_fn, session: Optional[Session]) -> None:
    def _do(sess: Session, target: Lead) -> None:
        md = dict(target.lead_metadata or {})
        existing = list(md.get("leadme_push_pending") or [])
        merged = merge_fn(existing, item)
        md["leadme_push_pending"] = merged
        # Preserve the earliest queued-at so operators can see how long
        # this lead has been waiting. Reset next_attempt to "soon" on
        # every fresh enqueue -- new information arrived, no reason to
        # keep waiting the full interval.
        md.setdefault("leadme_push_queued_at", _now_iso())
        md["leadme_push_next_attempt_at"] = _now_iso()  # try immediately on next tick
        md["leadme_push_attempts"] = int(md.get("leadme_push_attempts") or 0)
        # Explicitly clear any prior "gave up" flag -- a new push
        # supersedes an abandoned attempt.
        md.pop("leadme_push_abandoned", None)
        md.pop("leadme_push_abandoned_at", None)
        target.lead_metadata = md
        sess.flush()
        logger.info(
            "[leadme-queue] enqueued item {!r} for lead {} (phone={}, "
            "pending_count={})",
            item, target.id, target.phone, len(merged),
        )

    if session is not None:
        _do(session, lead)
        return
    with session_scope() as sess:
        # Re-fetch by id in the new session.
        fresh = sess.get(Lead, lead.id)
        if fresh is None:
            logger.warning(
                "[leadme-queue] cannot enqueue for detached lead id={} "
                "(no longer in DB?)", lead.id,
            )
            return
        _do(sess, fresh)


def _clear_pending(lead: Lead, remaining: list[dict]) -> None:
    """Update the lead's pending list. Called under an active session."""
    md = dict(lead.lead_metadata or {})
    now_iso = _now_iso()
    if remaining:
        md["leadme_push_pending"] = remaining
        md["leadme_push_next_attempt_at"] = _next_attempt_iso()
    else:
        # Full clear -- remove all queue keys so admin UI shows clean row.
        for k in (
            "leadme_push_pending",
            "leadme_push_next_attempt_at",
            "leadme_push_attempts",
            "leadme_push_queued_at",
            "leadme_push_abandoned",
            "leadme_push_abandoned_at",
        ):
            md.pop(k, None)
    md["leadme_push_last_tick_at"] = now_iso
    lead.lead_metadata = md


def _mark_attempt(lead: Lead, *, abandoned: bool = False) -> None:
    md = dict(lead.lead_metadata or {})
    md["leadme_push_attempts"] = int(md.get("leadme_push_attempts") or 0) + 1
    md["leadme_push_next_attempt_at"] = _next_attempt_iso()
    if abandoned:
        md["leadme_push_abandoned"] = True
        md["leadme_push_abandoned_at"] = _now_iso()
    lead.lead_metadata = md


# --- Retry driver -------------------------------------------------------


def _is_due(md: dict, now: datetime) -> bool:
    iso = md.get("leadme_push_next_attempt_at")
    if not iso:
        return True  # never scheduled == try now
    try:
        nxt = datetime.fromisoformat(iso)
    except ValueError:
        return True
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return now >= nxt


def _is_expired(md: dict, now: datetime) -> bool:
    """Have we been retrying this pending item for longer than the
    max lifetime? If yes, we give up on this cycle."""
    attempts = int(md.get("leadme_push_attempts") or 0)
    if attempts >= MAX_ATTEMPTS:
        return True
    iso = md.get("leadme_push_queued_at")
    if not iso:
        return False
    try:
        queued_at = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if queued_at.tzinfo is None:
        queued_at = queued_at.replace(tzinfo=timezone.utc)
    return now - queued_at >= timedelta(minutes=MAX_LIFETIME_MINUTES)


def _try_drain_via_v3(
    sess: Session, lead: Lead, md: dict, pending: list, now: datetime
) -> bool:
    """Try to drain engagement items via the v3 REST API.

    Returns True if we handled the queue (success OR permanent failure),
    False if v3 also can't find the lead yet (caller should fall back to
    cookie path or retry later).
    """
    from app.crm.leadme_v3 import get_lead_id, update_lead_status, add_lead_tag, LEVEL_STATUS_ID

    phone = lead.phone or ""
    lead_id = get_lead_id(phone)
    if not lead_id:
        # Still not in LeadMe -- leave for next tick.
        return False

    remaining: list[dict] = []
    for item in pending:
        kind = item.get("kind")
        if kind == "engagement":
            level = int(item.get("level") or 2)
            slot = item.get("slot")
            status_id = LEVEL_STATUS_ID.get(level)
            ok_status = True
            if status_id:
                ok_status = update_lead_status(lead_id, status_id)
            ok_tag = True
            if slot and slot not in ("any", "none"):
                tag = f"חלון · {slot}"
                ok_tag = add_lead_tag(lead_id, tag)
            if ok_status and ok_tag:
                logger.info(
                    "[leadme-queue v3] DRAINED engagement lead {} phone={} "
                    "leadId={} level={} slot={!r}",
                    lead.id, phone, lead_id, level, slot,
                )
            else:
                remaining.append(item)
        elif kind == "ctwa_tag":
            # v3 supports add_lead_tag directly -- no need for cookie path.
            campaign = (item.get("campaign") or "").strip()
            if not campaign:
                continue
            ok = add_lead_tag(lead_id, f"מקור: {campaign}")
            if ok:
                logger.info(
                    "[leadme-queue v3] DRAINED ctwa_tag lead {} phone={} "
                    "leadId={} campaign={!r}",
                    lead.id, phone, lead_id, campaign,
                )
            else:
                remaining.append(item)
        else:
            logger.warning(
                "[leadme-queue v3] unknown pending kind={!r} on lead {} "
                "-- dropping item", kind, lead.id,
            )

    _clear_pending(lead, remaining)
    return True


def _process_lead(sess: Session, lead: Lead, now: datetime) -> None:
    """Execute the pending items for a single lead, if any are due.

    All lookups + writes go through a single ``_build_client`` so we
    only pay the LeadMe session-priming cost once per lead per tick.
    """
    from app.crm.leadme_client import (
        BANNED_LEAKY_CAMPAIGN_ID,
        BANNED_LEAKY_CAMPAIGN_NAME,
        _admin_add_tag,
        _admin_change_status,
        _resolve_tag_lead_id,
        _status_id_for_level,
    )
    from app.crm.leadme_delete import _build_client, get_row_by_phone
    import re as _re

    md = dict(lead.lead_metadata or {})
    pending = list(md.get("leadme_push_pending") or [])
    if not pending:
        return
    if not _is_due(md, now):
        return

    if _is_expired(md, now):
        logger.error(
            "[leadme-queue] ABANDONING pending pushes for lead {} "
            "(phone={}, attempts={}, queued_at={}, items={}). "
            "Manual intervention required -- check that the lead exists "
            "in LeadMe and its phone matches.",
            lead.id, lead.phone,
            md.get("leadme_push_attempts"),
            md.get("leadme_push_queued_at"),
            pending,
        )
        _clear_pending(lead, [])
        # Also flip an abandoned flag so admin UI can badge it.
        md = dict(lead.lead_metadata or {})
        md["leadme_push_abandoned"] = True
        md["leadme_push_abandoned_at"] = _now_iso()
        md["leadme_push_abandoned_items"] = pending
        lead.lead_metadata = md
        return

    # If v3 API key is available, try draining via v3 first (no cookies needed).
    from app.config import get_settings as _get_settings
    if _get_settings().leadme_api_key:
        _drained = _try_drain_via_v3(sess, lead, md, pending, now)
        if _drained:
            return

    client = _build_client()
    if client is None:
        logger.warning(
            "[leadme-queue] no admin cookies; can't drain queue for "
            "lead {} (will retry next tick)", lead.id,
        )
        _mark_attempt(lead)
        return

    try:
        row = get_row_by_phone(lead.phone or "", client)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[leadme-queue] getRow raised for lead {} (phone={})",
            lead.id, lead.phone,
        )
        _mark_attempt(lead)
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        return

    if row is None:
        attempts_far = int(md.get("leadme_push_attempts") or 0) + 1
        logger.info(
            "[leadme-queue] lead {} (phone={}) STILL not found in LeadMe "
            "on attempt {}/{}; retrying in {}min",
            lead.id, lead.phone, attempts_far, MAX_ATTEMPTS,
            RETRY_INTERVAL_MINUTES,
        )
        _mark_attempt(lead)
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        return

    # We have a row! Resolve identifiers.
    lc_id = str(row[1]).strip() if len(row) > 1 else ""
    campaign = ""
    if len(row) > 4 and isinstance(row[4], str):
        campaign = _re.sub(r"<[^>]+>", " ", row[4])
        campaign = _re.sub(r"\s+", " ", campaign).strip()

    # HARD GUARD: if the lead now lives in the banned trash campaign,
    # refuse -- exactly the same rule as push_lead. Abandon quietly
    # (this is not a transient failure, so no point retrying).
    if (BANNED_LEAKY_CAMPAIGN_ID in (row[0] or "")
            or campaign == BANNED_LEAKY_CAMPAIGN_NAME):
        logger.error(
            "[leadme-queue SAFETY] REFUSING to drain queue for lead {} "
            "(phone={}) because it lives in banned campaign {!r}. "
            "Clearing pending items -- move the lead manually then "
            "re-trigger the bot flow.",
            lead.id, lead.phone,
            campaign or BANNED_LEAKY_CAMPAIGN_NAME,
        )
        _clear_pending(lead, [])
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        return

    if not lc_id or not lc_id.isdigit():
        logger.warning(
            "[leadme-queue] no numeric id in row for lead {} phone={} "
            "(row[1]={!r})", lead.id, lead.phone,
            row[1] if len(row) > 1 else None,
        )
        _mark_attempt(lead)
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        return

    # Resolve internal leadId lazily -- only if we have a tag to push.
    needs_tag = any(
        (it.get("kind") == "engagement" and it.get("slot"))
        or it.get("kind") == "ctwa_tag"
        for it in pending
    )
    tag_lead_id: Optional[str] = None
    if needs_tag:
        tag_lead_id = _resolve_tag_lead_id(client, lc_id)
        if tag_lead_id is None:
            # Couldn't resolve internal id -- can't add tags reliably.
            # Keep the pending items and retry next tick (viewLead might
            # be transiently returning a login page).
            logger.warning(
                "[leadme-queue] could not resolve internal leadId for "
                "lc_id={} (lead {} phone={}); postponing tag pushes",
                lc_id, lead.id, lead.phone,
            )
            _mark_attempt(lead)
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            return

    remaining: list[dict] = []
    for item in pending:
        kind = item.get("kind")
        try:
            if kind == "engagement":
                level = int(item.get("level") or 2)
                slot = item.get("slot")
                status_id = _status_id_for_level(level)
                ok_status = True
                if status_id:
                    ok_status = _admin_change_status(client, lc_id, status_id)
                ok_tag = True
                if slot and tag_lead_id:
                    ok_tag = _admin_add_tag(
                        client, tag_lead_id, f"חלון · {slot}",
                    )
                if ok_status and ok_tag:
                    logger.info(
                        "[leadme-queue] DRAINED engagement lead {} phone={} "
                        "lc_id={} level={} slot={!r} campaign={!r}",
                        lead.id, lead.phone, lc_id, level, slot, campaign,
                    )
                else:
                    logger.warning(
                        "[leadme-queue] partial fail on engagement lead "
                        "{}: status_ok={} tag_ok={} -- keeping item to "
                        "retry", lead.id, ok_status, ok_tag,
                    )
                    remaining.append(item)
            elif kind == "ctwa_tag":
                campaign_tag = (item.get("campaign") or "").strip()
                if not campaign_tag or tag_lead_id is None:
                    remaining.append(item)
                    continue
                ok = _admin_add_tag(
                    client, tag_lead_id, f"מקור: {campaign_tag}",
                )
                if ok:
                    logger.info(
                        "[leadme-queue] DRAINED ctwa_tag lead {} phone={} "
                        "campaign={!r}", lead.id, lead.phone, campaign_tag,
                    )
                else:
                    remaining.append(item)
            else:
                logger.warning(
                    "[leadme-queue] unknown pending kind={!r} on lead {} "
                    "-- dropping item", kind, lead.id,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[leadme-queue] item drain raised for lead {} item={}",
                lead.id, item,
            )
            remaining.append(item)

    # If any items still remain -> mark another attempt but don't
    # reset the "queued_at" clock (so the 60min lifetime keeps counting).
    if remaining:
        _mark_attempt(lead)
        _clear_pending(lead, remaining)
    else:
        _clear_pending(lead, [])

    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass


def retry_pending_pushes() -> None:
    """Scheduler entry point. Drains due items for every pending lead.

    Called from :mod:`app.followup.scheduler`. Safe to call by hand
    from a shell -- it opens/closes its own DB session.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as sess:
        # Find every lead that has any pending items. We can't easily
        # index on JSON key existence portably; a full scan of leads
        # with non-null metadata is fine at our volume (~thousands of
        # rows total).
        stmt = select(Lead).where(Lead.lead_metadata.isnot(None))
        leads = list(sess.execute(stmt).scalars().all())
        due: list[Lead] = []
        for lead in leads:
            md = lead.lead_metadata or {}
            pending = md.get("leadme_push_pending")
            if pending:
                due.append(lead)
        if not due:
            logger.info("[leadme-queue] tick: no pending items")
            return
        logger.info("[leadme-queue] tick: draining {} lead(s)", len(due))
        for lead in due:
            try:
                _process_lead(sess, lead, now)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[leadme-queue] _process_lead raised for lead {}",
                    lead.id,
                )


# --- Public introspection (for admin UI) --------------------------------


def get_queue_snapshot() -> list[dict[str, Any]]:
    """Return a snapshot list of pending leads for the admin UI.

    Each entry: ``{lead_id, phone, name, items, attempts, queued_at,
    next_attempt_at, abandoned}``. Read-only; safe to call from any
    request context.
    """
    out: list[dict[str, Any]] = []
    with session_scope() as sess:
        stmt = select(Lead).where(Lead.lead_metadata.isnot(None))
        for lead in sess.execute(stmt).scalars().all():
            md = lead.lead_metadata or {}
            pending = md.get("leadme_push_pending")
            abandoned = md.get("leadme_push_abandoned")
            if not pending and not abandoned:
                continue
            out.append({
                "lead_id": lead.id,
                "phone": lead.phone,
                "name": lead.name,
                "items": pending or md.get("leadme_push_abandoned_items") or [],
                "attempts": int(md.get("leadme_push_attempts") or 0),
                "queued_at": md.get("leadme_push_queued_at"),
                "next_attempt_at": md.get("leadme_push_next_attempt_at"),
                "abandoned": bool(abandoned),
                "abandoned_at": md.get("leadme_push_abandoned_at"),
            })
    out.sort(key=lambda r: (not r["abandoned"], r["queued_at"] or ""))
    return out
