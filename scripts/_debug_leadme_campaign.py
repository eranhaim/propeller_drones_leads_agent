"""Locate campaign leads endpoint by scraping the DataTables init in the campaign page."""
from __future__ import annotations
import re
import sys

from app.config import get_settings
from app.crm.leadme_delete import _build_client


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _debug_leadme_campaign.py <campaign_id>")
        return 2
    cid = sys.argv[1]
    c = _build_client()
    base = get_settings().leadme_admin_base
    page = c.get(base + f"/app/campaigns/manageCampaign/{cid}")
    html = page.text
    # dump every block referencing DataTable initialization
    idx = 0
    for m in re.finditer(r"DataTable|dataTable|sAjaxSource|ajax\s*:\s*\{|ajax\s*:\s*'|ajax\s*:\s*\"",
                         html):
        ctx = html[max(0, m.start() - 100):m.end() + 500]
        print(f"\n=== match {idx} @ {m.start()} ===")
        print(ctx.strip())
        idx += 1
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
