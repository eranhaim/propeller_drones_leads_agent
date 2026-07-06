"""Inbound HTTP webhook.

Purpose: LeadMe (via its "External Interfaces" mechanism) POSTs new leads
to this endpoint the moment they arrive on a campaign. We upsert the lead
in our own DB and immediately send them a warm WhatsApp opener via
GreenAPI so the user is engaged before the sales team ever picks up the
phone.

Endpoint:
    POST /webhook/leadme/{secret}

Accepts either application/json or application/x-www-form-urlencoded.
The exact field names LeadMe sends vary per external-interface config, so
we accept many common aliases (phone / phoneNumber / phonenumber /
tel / mobile, fullname / firstname+lastname / name, etc.).

Security: `{secret}` in the URL must match ``WEBHOOK_SECRET``. This is
the same "shared secret in the path" pattern used by Stripe, GitHub, and
LeadMe's own supplier API. If ``WEBHOOK_SECRET`` is empty, any request
that reaches the endpoint is accepted (dev-mode only).
"""

from __future__ import annotations

import re
from threading import Thread
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import get_settings
from app.webhook.opener import handle_new_lead


PHONE_KEYS = (
    "phone", "phoneNumber", "phonenumber", "phone_number",
    "tel", "telephone", "mobile", "cellphone", "cell",
)
NAME_KEYS = ("fullname", "full_name", "name")
FIRST_KEYS = ("firstname", "first_name", "givenname", "given_name")
LAST_KEYS = ("lastname", "last_name", "surname", "familyname", "family_name")
EMAIL_KEYS = ("email", "mail", "emailAddress", "email_address")
COMMENT_KEYS = ("comment", "comments", "note", "notes", "message")
CAMPAIGN_KEYS = ("campaignId", "campaign_id", "campaign", "campaignid")
LEAD_ID_KEYS = ("leadId", "lead_id", "id", "leadid")


def _first(payload: Dict[str, Any], keys) -> str:
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _normalize_phone(raw: str) -> str:
    """Best-effort E.164-without-plus normalization for Israeli numbers.

    Returns a digit-only string like ``972501234567`` suitable for building
    a WhatsApp chat id. Falls back to keeping just digits if the input
    can't be identified as Israeli.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("972"):
        return digits
    if digits.startswith("00972"):
        return digits[2:]
    if digits.startswith("0"):
        return "972" + digits[1:]
    if 8 <= len(digits) <= 9:
        return "972" + digits.lstrip("0")
    return digits


def _extract_custom_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pull out clf_XXXXX custom fields LeadMe forwards for the campaign
    (Propeller's on-form questions -- drone experience, course pick, ...).
    Also keeps any keys prefixed ``custom_`` or containing ``clf`` for
    forward compatibility."""
    extras: Dict[str, Any] = {}
    for k, v in payload.items():
        if v in (None, ""):
            continue
        low = k.lower()
        if low.startswith("clf_") or low.startswith("clf[") or low.startswith("custom_"):
            extras[k] = v
    return extras


def _flatten_payload(raw: Any) -> Dict[str, Any]:
    """Handle both dict payloads and single-lead-in-list variants."""
    if isinstance(raw, dict):
        # LeadMe sometimes wraps under {"data": {...}} or {"lead": {...}}
        for wrapper in ("data", "lead", "leadData"):
            inner = raw.get(wrapper)
            if isinstance(inner, dict):
                merged = {**raw, **inner}
                merged.pop(wrapper, None)
                return merged
        return raw
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return raw[0]
    return {}


app = FastAPI(title="Propeller Drones lead webhook", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/leadme/{secret}")
async def leadme_webhook(secret: str, request: Request) -> JSONResponse:
    settings = get_settings()
    expected = (settings.webhook_secret or "").strip()
    if expected and secret != expected:
        logger.warning("Rejected LeadMe webhook: bad secret ({} chars)", len(secret))
        raise HTTPException(status_code=403, detail="bad secret")

    content_type = (request.headers.get("content-type") or "").lower()
    raw_payload: Any
    try:
        if "application/json" in content_type:
            raw_payload = await request.json()
        else:
            form = await request.form()
            raw_payload = dict(form)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to parse webhook body: {}", e)
        body = (await request.body())[:400]
        logger.error("raw body preview: {!r}", body)
        raise HTTPException(status_code=400, detail="invalid body")

    payload = _flatten_payload(raw_payload)
    logger.info("[LeadMe webhook] payload keys={}", list(payload.keys()))
    logger.debug("[LeadMe webhook] full payload: {}", payload)

    raw_phone = _first(payload, PHONE_KEYS)
    phone = _normalize_phone(raw_phone)
    if not phone:
        logger.error("[LeadMe webhook] no phone in payload, ignoring. keys={}",
                     list(payload.keys()))
        return JSONResponse({"status": "ignored", "reason": "no phone"},
                            status_code=200)

    # Assemble a display name from available fields.
    name = _first(payload, NAME_KEYS)
    if not name:
        first = _first(payload, FIRST_KEYS)
        last = _first(payload, LAST_KEYS)
        name = f"{first} {last}".strip()

    email = _first(payload, EMAIL_KEYS)
    comment = _first(payload, COMMENT_KEYS)
    campaign_id = _first(payload, CAMPAIGN_KEYS)
    leadme_lead_id = _first(payload, LEAD_ID_KEYS)

    metadata: Dict[str, Any] = {
        "leadme_campaign_id": campaign_id,
        "leadme_lead_id": leadme_lead_id,
        "leadme_raw_comment": comment,
        "email": email,
    }
    metadata.update(_extract_custom_fields(payload))
    # Drop empties to keep the JSON tidy
    metadata = {k: v for k, v in metadata.items() if v not in (None, "")}

    # Run the (potentially slow) opener in a background thread so LeadMe
    # gets an immediate 200 and doesn't retry.
    Thread(
        target=handle_new_lead,
        kwargs={
            "phone": phone,
            "name": name or None,
            "metadata": metadata,
            "campaign_id": campaign_id or None,
        },
        daemon=True,
        name=f"opener-{phone}",
    ).start()

    return JSONResponse(
        {"status": "accepted", "phone": phone, "name": name},
        status_code=200,
    )


def run_in_background_thread() -> None:
    """Start uvicorn on a daemon thread so ``main.py`` can then call
    ``bot.run_forever()`` on the main thread. Keeps the container as a
    single process."""
    import uvicorn

    settings = get_settings()
    port = settings.webhook_port
    logger.info("Starting webhook server on 0.0.0.0:{}", port)

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level=settings.log_level.lower(),
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    Thread(target=server.run, daemon=True, name="webhook-uvicorn").start()
