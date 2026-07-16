"""LeadMe internal-admin delete-by-phone.

The public "supplier" API in :mod:`app.crm.leadme_client` can only INSERT
and UPDATE. To DELETE a lead (needed when the admin panel wipes a lead
for manual QA -- the same phone can then re-submit the LeadMe form as a
brand-new lead), we have to talk to LeadMe's INTERNAL admin endpoints
that their web UI uses. Those endpoints require:

- an authenticated PHP session cookie (``PHPSESSID``), captured once
  from a logged-in browser and mounted into the container as a JSON file
  (see ``LEADME_COOKIES_PATH``);
- a CodeIgniter CSRF token, echoed back as the ``csrf_lmcms`` form
  field. LeadMe stores it in a cookie named ``csrf_cookie_name``.

The DELETE flow is two calls:

1. GET ``/app/ajax/getDataForTable?...&search[value]=<phone>`` -- returns
   a DataTables-style JSON with rows. The first cell in each row is an
   HTML fragment containing ``<input name="selectedLeads[]"
   value="<leadme_id>">`` -- that's the numeric ID we need.
2. POST ``/app/ajax/deleteLeads`` with ``data[leadId][]=<leadme_id>`` and
   ``csrf_lmcms=<token>``. Response: ``{"result": true, "data": "<id>"}``.

If any step fails we return ``(False, reason)`` and the caller keeps
going with the local DB delete -- we do NOT block the admin's local
wipe just because LeadMe's session expired.

Cookies expire (PHPSESSID typically lives for a session; CSRF cookie
lasts hours-to-days). When that happens the admin panel will still work
locally, and this module will log a WARNING that the cookies need to
be re-exported. Regenerating them is a one-off manual step (Chrome
DevTools -> Application -> Cookies -> export).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from app.config import get_settings


_LEADME_ID_RE = re.compile(r'name=[\"\']selectedLeads\[\]?[\"\']\s+value=[\"\'](\d+)[\"\']')

# LeadMe's DataTables endpoint requires the full ``columns`` array or it
# 500s. We send the same 8 empty columns their UI sends -- see the URL
# the customer captured from DevTools. Only ``search[value]`` and ``_``
# (cache-buster) actually vary per call.
def _search_params(phone: str) -> list[tuple[str, str]]:
    """Build the DataTables query for the leads list search."""
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
        ("length", "10"),
        ("search[value]", phone),
        ("search[regex]", "false"),
        ("_", str(int(time.time() * 1000))),
    ])
    return p


def _load_cookies_file() -> Optional[list[dict]]:
    settings = get_settings()
    raw = (settings.leadme_cookies_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.exists():
        logger.warning(
            "[leadme-delete] LEADME_COOKIES_PATH={!r} does not exist -- "
            "LeadMe delete disabled. Local DB delete will still work.",
            raw,
        )
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("[leadme-delete] failed to parse cookies file {}",
                         raw)
        return None


def _build_client() -> Optional[httpx.Client]:
    """Return an httpx.Client with LeadMe cookies pre-loaded, or None."""
    cookies = _load_cookies_file()
    if not cookies:
        return None

    jar = httpx.Cookies()
    csrf_token: Optional[str] = None
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain", "").lstrip(".") or "www.leadmecms.co.il"
        # Only carry cookies for the LeadMe admin domain; the browser
        # export inevitably includes Google/analytics junk we don't want.
        if "leadmecms.co.il" not in domain:
            continue
        jar.set(name, value, domain=domain, path=c.get("path", "/"))
        if name == "csrf_cookie_name":
            csrf_token = value

    if csrf_token is None:
        logger.warning(
            "[leadme-delete] no csrf_cookie_name cookie found; LeadMe "
            "delete will likely be rejected as CSRF failure"
        )

    client = httpx.Client(
        cookies=jar,
        timeout=15.0,
        follow_redirects=False,
        headers={
            # LeadMe's admin endpoints are XHR-ish; sending these headers
            # matches what the browser sends and avoids a redirect to a
            # login page (which we would then treat as a 200 and blow up
            # when parsing JSON).
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Referer": get_settings().leadme_admin_base + "/app/leads",
        },
    )
    # Stash the CSRF token on the client for downstream calls.
    client.__dict__["_csrf_token"] = csrf_token
    return client


def find_leadme_id_by_phone(phone: str, client: httpx.Client) -> Optional[str]:
    """Return the numeric LeadMe lead id for ``phone``, or None.

    Prime the session by visiting ``/app/leads`` first (LeadMe's admin
    stores a per-tab "active datatable" in the session; calls to the
    ajax endpoint 302 -> /404 without it). Then call
    ``/app/leads/getDataForTable`` -- reverse-engineered from the actual
    working URL rather than the /app/ajax/getDataForTable path that
    appears in some old DevTools captures (which redirects to /404 for
    us now).
    """
    settings = get_settings()
    base = settings.leadme_admin_base
    # Prime the session -- without this the ajax endpoint 302s to /404.
    try:
        client.get(base + "/app/leads")
    except httpx.HTTPError:
        pass  # not fatal, try the ajax call anyway
    url = base + "/app/leads/getDataForTable"
    params = _search_params(phone)
    try:
        resp = client.get(url, params=params)
    except httpx.HTTPError as e:
        logger.error("[leadme-delete] search request failed: {}", e)
        return None

    if resp.status_code != 200:
        logger.warning(
            "[leadme-delete] search HTTP {} for phone={} (session cookie "
            "may be expired) -- body preview: {!r}",
            resp.status_code, phone, resp.text[:200],
        )
        return None

    try:
        body = resp.json()
    except json.JSONDecodeError:
        # A redirect to the login page comes back as HTML, not JSON -- the
        # canonical "your cookies expired" signal.
        logger.warning(
            "[leadme-delete] search returned non-JSON for phone={} -- "
            "cookies likely expired. First 200 chars: {!r}",
            phone, resp.text[:200],
        )
        return None

    rows = body.get("data") or []
    for row in rows:
        # Row is an array of HTML cells; first cell has the checkbox with
        # the leadme id as ``value=""``.
        cell = row[0] if row and isinstance(row, list) else None
        if not isinstance(cell, str):
            continue
        m = _LEADME_ID_RE.search(cell)
        if m:
            leadme_id = m.group(1)
            logger.info(
                "[leadme-delete] resolved phone={} -> leadme_id={}",
                phone, leadme_id,
            )
            return leadme_id

    logger.info("[leadme-delete] no LeadMe lead found for phone={}", phone)
    return None


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def get_row_by_phone(phone: str, client: httpx.Client) -> Optional[list]:
    """Return the raw DataTables row for ``phone`` (list of cell HTML), or None.

    Same underlying request as :func:`find_leadme_id_by_phone` but returns the
    full row so callers can inspect other cells (status column, campaign,
    etc.) without a second HTTP round-trip.
    """
    settings = get_settings()
    base = settings.leadme_admin_base
    try:
        client.get(base + "/app/leads")
    except httpx.HTTPError:
        pass
    url = base + "/app/leads/getDataForTable"
    try:
        resp = client.get(url, params=_search_params(phone))
    except httpx.HTTPError as e:
        logger.error("[leadme-status] search request failed: {}", e)
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except json.JSONDecodeError:
        return None
    rows = body.get("data") or []
    if not rows:
        return None
    row = rows[0]
    if isinstance(row, list):
        return row
    return None


def get_current_status_text(phone: str, client: httpx.Client) -> Optional[str]:
    """Return the status column plain-text ("חדש", "חדש - רמה 1", ...) or None.

    Scans every cell of the row, strips HTML tags, and returns the first
    cell whose text contains a known status keyword. If nothing looks like
    a status, returns the concatenated stripped text of all cells (still
    useful — the caller can .contains-check whatever it needs).
    """
    row = get_row_by_phone(phone, client)
    if row is None:
        return None

    status_keywords = ("חדש", "מעוניין", "לא רלוונטי", "לא ענה",
                       "עסקה", "בטיפול", "נדחה", "נסגר")
    texts = []
    for cell in row:
        if not isinstance(cell, str):
            continue
        text = _HTML_TAG_RE.sub(" ", cell)
        text = re.sub(r"\s+", " ", text).strip()
        texts.append(text)
        if any(kw in text for kw in status_keywords):
            return text
    return " | ".join(texts) if texts else None


def delete_leadme_id(leadme_id: str, client: httpx.Client) -> tuple[bool, str]:
    """Fire the deleteLeads POST. Returns (ok, detail).

    Tries both known endpoint paths (LeadMe's admin has moved things
    around between versions) and both known payload keys (the DevTools
    screenshots we have from two different points in time show two
    different names: ``data[leadId][]`` and ``leadIds[]``).
    """
    settings = get_settings()
    base = settings.leadme_admin_base
    csrf = client.cookies.get("csrf_cookie_name") \
        or client.__dict__.get("_csrf_token") or ""

    last_detail = "no attempts"
    for url in [base + "/app/ajax/deleteLeads",
                base + "/app/leads/deleteLeads"]:
        for payload_key in ["data[leadId][]", "leadIds[]"]:
            # Refresh CSRF each attempt in case the framework rotated it.
            csrf_now = client.cookies.get("csrf_cookie_name") or csrf
            data = [(payload_key, leadme_id), ("csrf_lmcms", csrf_now)]
            try:
                resp = client.post(url, data=data)
            except httpx.HTTPError as e:
                last_detail = f"httpx error at {url}: {e}"
                continue
            if resp.status_code != 200:
                last_detail = (f"HTTP {resp.status_code} at {url} "
                               f"key={payload_key}: {resp.text[:120]}")
                continue
            try:
                body = resp.json()
            except json.JSONDecodeError:
                last_detail = (f"non-JSON at {url} key={payload_key} "
                               f"(session likely expired): {resp.text[:120]}")
                continue
            if body.get("result") is True or body.get("type") == "success":
                return True, (f"deleted leadme_id={leadme_id} via {url} "
                              f"(key={payload_key})")
            last_detail = f"rejected by {url} key={payload_key}: {body!r}"

    return False, last_detail


def delete_from_leadme(phone: str) -> tuple[bool, str]:
    """High-level: find lead by phone and delete it. Best-effort.

    Never raises. On any failure returns ``(False, reason)`` and the
    admin's local-DB delete still proceeds -- the worst-case outcome is
    "lead is gone locally but still in LeadMe", which we can clean up
    manually.
    """
    client = _build_client()
    if client is None:
        return False, "no LeadMe cookies configured"

    try:
        leadme_id = find_leadme_id_by_phone(phone, client)
        if leadme_id is None:
            return False, "phone not found in LeadMe"
        return delete_leadme_id(leadme_id, client)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
