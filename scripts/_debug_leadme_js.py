"""Extract the exact changeStatus JS body from /app/leads HTML."""
from __future__ import annotations
import re

from app.crm.leadme_delete import _build_client
from app.config import get_settings


def main() -> int:
    c = _build_client()
    if c is None:
        print("no cookies")
        return 2
    base = get_settings().leadme_admin_base
    html = c.get(base + "/app/leads").text
    # find the JS click handler for .changeStatusPuBtn (last occurrence in file)
    idx = html.rfind(".changeStatusPuBtn")
    if idx < 0:
        print("not found")
        return 2
    print(html[max(0, idx - 200):idx + 3000])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
