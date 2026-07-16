"""Debug helper: try multiple DataTables endpoints for a phone."""
from __future__ import annotations
import sys
import time

from app.config import get_settings
from app.crm.leadme_delete import _build_client, _search_params


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _debug_leadme_row.py <phone>")
        return 2
    client = _build_client()
    if client is None:
        print("no LeadMe cookies")
        return 2
    base = get_settings().leadme_admin_base
    prime = client.get(base + "/app/leads")
    print(f"PRIME {prime.status_code} url={prime.url}")
    time.sleep(0.5)
    phone = sys.argv[1]
    import json as _json
    path = "/app/ajax/getDataForTable"
    variant = phone[-9:]  # last 9 digits = Israeli local w/o leading 0
    print(f"\n=== search={variant!r} ===")
    resp = client.get(base + path, params=_search_params(variant))
    body = _json.loads(resp.text)
    rows = body.get("data") or []
    print(f"rows: {len(rows)}")
    if rows:
        row = rows[0]
        for i, cell in enumerate(row):
            print(f"--- cell[{i}] ---")
            print(cell if isinstance(cell, str) else repr(cell))
    return 0


if __name__ == "__main__":
    sys.exit(main())
