"""Fetch a LeadMe lead's edit page and dump status + tags fields."""
from __future__ import annotations
import re
import sys

from app.config import get_settings
from app.crm.leadme_delete import _build_client


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _debug_leadme_lead.py <leadme_numeric_id>")
        return 2
    lead_id = sys.argv[1]
    client = _build_client()
    if client is None:
        print("no cookies")
        return 2
    base = get_settings().leadme_admin_base
    try:
        client.get(base + "/app/leads")
    except Exception:
        pass
    for path in [f"/app/leads/edit/{lead_id}",
                 f"/app/leads/details/{lead_id}",
                 f"/app/ajax/getLeadSummary/{lead_id}"]:
        resp = client.get(base + path)
        print(f"\n=== {path} -> HTTP {resp.status_code} "
              f"content-type={resp.headers.get('content-type')} "
              f"len={len(resp.text)} ===")
        text = resp.text
        # Print any element around 'רמה', 'tag', 'status'
        for kw in ["חדש - רמה", "PROBE-", "tags", "רמה 1", "רמה 2", "רמה 3",
                   "DEBUG-PROBE", 'name="status"', "selected"]:
            for m in re.finditer(re.escape(kw), text):
                start = max(0, m.start() - 80)
                end = min(len(text), m.end() + 120)
                print(f"  [{kw}] ...{text[start:end]}...")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
