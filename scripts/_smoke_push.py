"""Trigger push_lead for a phone from the DB and check campaign 12277 count before/after."""
from __future__ import annotations
import json
import sys

from sqlalchemy import select

from app.config import get_settings
from app.crm.leadme_client import push_lead
from app.crm.leadme_delete import _build_client
from app.db.models import Lead
from app.db.session import session_scope


def _campaign_count(client, cid: str) -> int:
    base = get_settings().leadme_admin_base
    client.get(base + f"/app/campaigns/manageCampaign/{cid}")
    csrf = client.cookies.get("csrf_cookie_name") \
        or client.__dict__.get("_csrf_token") or ""
    r = client.post(base + "/app/ajax4/getPieData",
                    data={"campaignId": cid,
                          "startDate": "01/06/2020",
                          "endDate": "31/12/2030",
                          "csrf_lmcms": csrf})
    body = json.loads(r.text)
    return sum(body.get("pieData", {}).get("dataD") or [0])


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _smoke_push.py <phone>")
        return 2
    phone = sys.argv[1]
    c = _build_client()
    before = _campaign_count(c, "12277")
    print(f"BEFORE: campaign 12277 lead count = {before}")
    c.close()

    with session_scope() as s:
        lead = s.execute(select(Lead).where(Lead.phone == phone)).scalar_one_or_none()
        if lead is None:
            print(f"no lead in DB for phone {phone}")
            return 1
        ok = push_lead(lead, note="smoke test", level=1)
        print(f"push_lead(level=1) -> {ok}")

    c = _build_client()
    after = _campaign_count(c, "12277")
    print(f"AFTER: campaign 12277 lead count = {after}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
