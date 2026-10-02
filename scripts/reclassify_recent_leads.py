"""Audit and safely reconcile the last 30 days of LeadMe classifications.

Run a dry report first. Apply only that report after reviewing its validation:

    python scripts/reclassify_recent_leads.py --report /app/data/reports/l1-l3-dry.json
    python scripts/reclassify_recent_leads.py --apply-report /app/data/reports/l1-l3-dry.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.crm.leadme_client import _is_test_phone
from app.crm.leadme_v3 import (
    get_lead_status,
    is_v3_available,
    level_for_status_id,
    update_lead_status,
)
from app.crm.levels import classify_engagement
from app.db.models import Lead
from app.db.session import session_scope


_MANAGED_STATUS_TITLES = {"חדש", "חדש - רמה 1", "חדש - רמה 2", "חדש - רמה 3"}
_MAX_MISSING_RATIO = 0.10


def _redact_phone(phone: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    return f"***{digits[-2:]}" if len(digits) >= 2 else "***"


def _redact_id(namespace: str, value: int) -> str:
    """Return an auditable, non-identifying reference for an internal ID."""
    digest = hashlib.sha256(f"{namespace}:{value}".encode()).hexdigest()
    return f"{namespace}-{digest[:12]}"


def _lead_ref(lead_id: int) -> str:
    return _redact_id("lead", lead_id)


def _status_summary(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if status is None:
        return None
    return {
        "leadme_ref": _redact_id("leadme", status["leadId"]),
        "status_id": status["status"],
        "status_title": status["statusTitle"],
        "level": level_for_status_id(status["status"]),
    }


def _is_protected(lead: Lead, status: dict[str, Any]) -> str | None:
    if lead.bot_muted:
        return "bot_muted"
    if (lead.lead_metadata or {}).get("leadme_relevance") == "not_relevant":
        return "explicit_not_interested"
    if status["statusTitle"].strip() not in _MANAGED_STATUS_TITLES:
        return "manual_or_sales_status"
    return None


def _record_applied_level(lead: Lead, level: int) -> None:
    metadata = dict(lead.lead_metadata or {})
    metadata["leadme_last_level"] = level
    lead.lead_metadata = metadata


def _dry_run(days: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    remote_reads = 0
    remote_missing = 0

    with session_scope() as session:
        query = (
            select(Lead)
            .options(selectinload(Lead.messages))
            .where(or_(Lead.created_at >= cutoff, Lead.last_message_at >= cutoff))
            .order_by(Lead.id)
        )
        for lead in session.execute(query).scalars():
            record: dict[str, Any] = {
                "lead_ref": _lead_ref(lead.id),
                "phone": _redact_phone(lead.phone),
                "target": None,
                "reason": None,
                "before": None,
                "action": "skip",
                "skip_reason": None,
            }
            if _is_test_phone(lead.phone):
                record["skip_reason"] = "test_phone"
            else:
                decision = classify_engagement(lead, lead.messages)
                record["target"] = decision.level
                record["reason"] = decision.reason
                if decision.level is None:
                    record["skip_reason"] = "explicit_not_interested"
                else:
                    status = get_lead_status(phone=lead.phone)
                    if status is None:
                        remote_missing += 1
                        record["skip_reason"] = "not_found_or_unreadable_in_leadme"
                    else:
                        remote_reads += 1
                        record["before"] = _status_summary(status)
                        protected = _is_protected(lead, status)
                        if protected:
                            record["skip_reason"] = protected
                        elif level_for_status_id(status["status"]) == decision.level:
                            record["skip_reason"] = "already_matching"
                        else:
                            record["action"] = "change"
            counts[record["action"] if record["action"] == "change" else record["skip_reason"] or "skip"] += 1
            records.append(record)

    candidate_count = len(records)
    missing_ratio = remote_missing / max(1, remote_reads + remote_missing)
    validation = {
        "v3_available": is_v3_available(),
        "candidates": candidate_count,
        "remote_reads": remote_reads,
        "remote_missing": remote_missing,
        "remote_missing_ratio": round(missing_ratio, 4),
        "ready_to_apply": bool(remote_reads) and missing_ratio <= _MAX_MISSING_RATIO,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "cutoff": cutoff.isoformat(),
        "counts": dict(sorted(counts.items())),
        "validation": validation,
    }, records


def _write_report(path: Path, summary: dict[str, Any], records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"summary": summary, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _apply_report(path: Path) -> dict[str, int]:
    report = json.loads(path.read_text(encoding="utf-8"))
    validation = report.get("summary", {}).get("validation", {})
    if not validation.get("ready_to_apply"):
        raise ValueError("dry-run validation was not ready; refusing to write")

    planned = [record for record in report.get("records", []) if record.get("action") == "change"]
    outcomes: Counter[str] = Counter()

    # Read every planned row again before the first write. A sales rep may have
    # changed a status after the report; that row must be skipped, not overwritten.
    preflight: list[tuple[int, dict[str, Any], int]] = []
    with session_scope() as session:
        leads_by_ref = {
            _lead_ref(lead.id): lead
            for lead in session.execute(select(Lead)).scalars()
        }
        for record in planned:
            lead = leads_by_ref.get(record.get("lead_ref"))
            if lead is None:
                outcomes["missing_local_lead"] += 1
                continue
            status = get_lead_status(phone=lead.phone)
            before = record.get("before") or {}
            if (
                status is None
                or _redact_id("leadme", status["leadId"]) != before.get("leadme_ref")
                or status["status"] != before.get("status_id")
            ):
                outcomes["changed_since_dry_run"] += 1
                continue
            protected = _is_protected(lead, status)
            if protected:
                outcomes[protected] += 1
                continue
            preflight.append((lead.id, status, int(record["target"])))

    for lead_id, status, target in preflight:
        if not update_lead_status(status["leadId"], target_status_id := _status_id(target)):
            outcomes["write_or_readback_failed"] += 1
            continue
        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None:
                outcomes["local_lead_removed_after_write"] += 1
                continue
            _record_applied_level(lead, target)
        outcomes["applied"] += 1
    outcomes["planned"] = len(planned)
    outcomes["preflight_ready"] = len(preflight)
    return dict(outcomes)


def _status_id(level: int) -> int:
    from app.crm.leadme_v3 import status_id_for_level

    status_id = status_id_for_level(level)
    if status_id is None:
        raise ValueError(f"Level {level} has no configured LeadMe status ID")
    return status_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply-report", type=Path)
    args = parser.parse_args(argv)

    if bool(args.report) == bool(args.apply_report):
        parser.error("use exactly one of --report or --apply-report")
    if not is_v3_available():
        print("ERROR: LeadMe v3 must be available for verified reconciliation.")
        return 2

    if args.report:
        summary, records = _dry_run(args.days)
        _write_report(args.report, summary, records)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"report={args.report}")
        return 0

    try:
        outcomes = _apply_report(args.apply_report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(outcomes, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
