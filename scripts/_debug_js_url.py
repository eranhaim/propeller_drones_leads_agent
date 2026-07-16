"""Fetch the manageCampaign JS and look for the DataTables ajax URL."""
from __future__ import annotations
from app.crm.leadme_delete import _build_client


def main() -> int:
    c = _build_client()
    r = c.get("https://www.leadmecms.co.il/assets/js/app/manageCampaign_.js?v=20260716")
    print(f"HTTP {r.status_code} len={len(r.text)}")
    for line in r.text.splitlines():
        low = line.lower()
        if any(k in low for k in ("ajax", "url:", "geturl", "getdata",
                                   "campaigns/", "leads/get")):
            print(line.strip())
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
