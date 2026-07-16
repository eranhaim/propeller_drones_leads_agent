"""Probe /app/leads/changeLeadsStatus with the correct payload shape."""
from __future__ import annotations
import sys

from app.config import get_settings
from app.crm.leadme_delete import _build_client


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: _debug_leadme_update.py <leadme_numeric_id> <status_rel_id>")
        return 2
    lead_id = sys.argv[1]
    status_id = sys.argv[2]
    c = _build_client()
    if c is None:
        print("no cookies")
        return 2
    base = get_settings().leadme_admin_base
    try:
        c.get(base + "/app/leads")
    except Exception:
        pass
    csrf = c.cookies.get("csrf_cookie_name") \
        or c.__dict__.get("_csrf_token") or ""
    url = base + "/app/leads/changeLeadsStatus"

    payloads = [
        # The JS builds { data: { status: X, leadId: "id,id,id" } }
        # which jQuery serializes as data[status] + data[leadId].
        {"data[status]": status_id, "data[leadId]": lead_id,
         "csrf_lmcms": csrf},
    ]
    for i, payload in enumerate(payloads, 1):
        try:
            resp = c.post(url, data=payload)
        except Exception as e:
            print(f"\n--- payload#{i} ---\nerror: {e}")
            continue
        print(f"\n--- payload#{i} keys={list(payload)} ---")
        print(f"HTTP {resp.status_code} content-type={resp.headers.get('content-type')}")
        print(f"body: {resp.text[:400]!r}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
