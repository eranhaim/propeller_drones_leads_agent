"""Probe admin endpoints for tags & comments."""
from __future__ import annotations
import sys

from app.crm.leadme_delete import _build_client


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _debug_admin_endpoints.py <leadme_id>")
        return 2
    leadme_id = sys.argv[1]
    c = _build_client()
    base = "https://www.leadmecms.co.il"
    c.get(base + "/app/leads")
    csrf = c.cookies.get("csrf_cookie_name") or c.__dict__.get("_csrf_token") or ""

    endpoints = [
        ("/app/ajax/addLeadTag",     {"text": "probe-tag-x", "leadId": leadme_id,
                                       "csrf_lmcms": csrf}),
        ("/app/ajax/addTag",         {"text": "probe-tag-y", "leadId": leadme_id,
                                       "csrf_lmcms": csrf}),
        ("/app/leads/addTag",        {"text": "probe-tag-z", "leadId": leadme_id,
                                       "csrf_lmcms": csrf}),
        ("/app/ajax/addLeadComment", {"text": "probe-comment-x", "leadId": leadme_id,
                                       "csrf_lmcms": csrf}),
        ("/app/ajax/addComment",     {"text": "probe-comment-y", "leadId": leadme_id,
                                       "csrf_lmcms": csrf}),
    ]
    for path, payload in endpoints:
        r = c.post(base + path, data=payload)
        print(f"{path}: HTTP {r.status_code} body: {r.text[:200]!r}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
