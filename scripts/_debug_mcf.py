"""Search each recently-pushed phone and print ALL matching LeadMe rows."""
from __future__ import annotations
import json
import re
import time

from app.crm.leadme_delete import _build_client, _phone_variants


def _params(term: str) -> list[tuple[str, str]]:
    p: list[tuple[str, str]] = []
    for i in range(8):
        p.extend([
            (f"columns[{i}][data]", str(i)),
            (f"columns[{i}][name]", ""),
            (f"columns[{i}][searchable]", "true"),
            (f"columns[{i}][orderable]", "true"),
            (f"columns[{i}][search][value]", ""),
            (f"columns[{i}][search][regex]", "false"),
        ])
    p.extend([
        ("order[0][column]", "2"),
        ("order[0][dir]", "desc"),
        ("start", "0"),
        ("length", "50"),
        ("search[value]", term),
        ("search[regex]", "false"),
        ("_", str(int(time.time() * 1000))),
    ])
    return p


PHONES = [
    "972546822848", "972529250283", "972509012907", "972526821017",
    "972524816023", "972508376234", "972543014650", "972543027343",
    "972543102222", "972533321720",
]


def main() -> int:
    c = _build_client()
    base = "https://www.leadmecms.co.il"
    c.get(base + "/app/leads")
    for phone in PHONES:
        variant = phone[-9:]
        r = c.get(base + "/app/ajax/getDataForTable", params=_params(variant))
        try:
            body = json.loads(r.text)
        except Exception:
            print(f"{phone}: json parse err")
            continue
        rows = body.get("data") or []
        campaigns = []
        for row in rows:
            if not isinstance(row, list):
                continue
            lid = row[1] if len(row) > 1 else "?"
            camp = row[4] if len(row) > 4 else ""
            camp_txt = re.sub(r"<[^>]+>", " ", camp)
            camp_txt = re.sub(r"\s+", " ", camp_txt).strip()
            campaigns.append((lid, camp_txt))
        print(f"{phone} ({variant}): {len(rows)} rows -> {campaigns}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
