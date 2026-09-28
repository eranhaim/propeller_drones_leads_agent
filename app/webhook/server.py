"""Inbound HTTP webhook.

Purpose: LeadMe (via its "External Interfaces" mechanism) POSTs new leads
to this endpoint the moment they arrive on a campaign. We upsert the lead
in our own DB. Approved website-form sources receive one warm WhatsApp
opener via GreenAPI; other sources remain CRM-only.

Endpoint:
    POST /webhook/leadme/{secret}

Accepts either application/json or application/x-www-form-urlencoded. The
configured LeadMe fields are normalized from their casing and array variants:
phone, firstname, lastname, campaign, tags, Facebook Lead id, and sourceType.

Security: `{secret}` in the URL must match ``WEBHOOK_SECRET``. This is
the same "shared secret in the path" pattern used by Stripe, GitHub, and
LeadMe's own supplier API. If ``WEBHOOK_SECRET`` is empty, any request
that reaches the endpoint is accepted (dev-mode only).
"""

from __future__ import annotations

import time
from threading import Lock, Thread
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from app.admin.routes import admin_session_secret, router as admin_router
from app.config import get_settings
from app.webhook.opener import handle_new_lead
from app.webhook.payload import (
    flatten_payload as _flatten_payload,
    is_valid_whatsapp_phone as _is_valid_whatsapp_phone,
    normalize_leadme_payload,
    normalize_phone as _normalize_phone,
)


_polling_lock = Lock()
_polling_started_at: Optional[float] = None
_polling_last_success_at: Optional[float] = None
_polling_last_error: Optional[str] = None


def mark_polling_started() -> None:
    """Record entry into a GreenAPI receive request."""
    global _polling_started_at
    with _polling_lock:
        _polling_started_at = time.monotonic()


def mark_polling_success() -> None:
    """Record a successful GreenAPI receive request (including empty queue)."""
    global _polling_last_success_at, _polling_last_error
    with _polling_lock:
        _polling_last_success_at = time.monotonic()
        _polling_last_error = None


def mark_polling_error(error: str) -> None:
    """Keep the last safe error summary for the liveness endpoint."""
    global _polling_last_error
    with _polling_lock:
        _polling_last_error = error[:200]


def polling_seconds_since_success() -> Optional[float]:
    with _polling_lock:
        if _polling_last_success_at is None:
            if _polling_started_at is None:
                return None
            return time.monotonic() - _polling_started_at
        return time.monotonic() - _polling_last_success_at


def _polling_health() -> Dict[str, Any]:
    age = polling_seconds_since_success()
    settings = get_settings()
    healthy = age is not None and age <= settings.polling_watchdog_seconds
    with _polling_lock:
        last_error = _polling_last_error
    result: Dict[str, Any] = {
        "status": "ok" if healthy else "degraded",
        "polling": "healthy" if healthy else "stale",
    }
    if age is not None:
        result["poll_age_seconds"] = round(age, 1)
    if last_error:
        result["poll_error"] = last_error
    return result


app = FastAPI(title="Propeller Drones lead webhook", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=admin_session_secret(get_settings().admin_password),
    session_cookie="propeller_admin_session",
    max_age=8 * 60 * 60,
    same_site="strict",
    https_only=True,
)
app.include_router(admin_router)


@app.get("/health")
def health() -> JSONResponse:
    payload = _polling_health()
    status_code = 200 if payload["status"] == "ok" else 503
    return JSONResponse(payload, status_code=status_code)


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
            raw_payload = {}
            for key, value in form.multi_items():
                existing = raw_payload.get(key)
                if existing is None:
                    raw_payload[key] = value
                elif isinstance(existing, list):
                    existing.append(value)
                else:
                    raw_payload[key] = [existing, value]
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to parse webhook body: {}", e)
        raise HTTPException(status_code=400, detail="invalid body")

    payload = _flatten_payload(raw_payload)
    normalized = normalize_leadme_payload(payload)
    logger.info("[LeadMe webhook] payload keys={}", list(payload.keys()))

    if not _is_valid_whatsapp_phone(normalized.phone):
        logger.warning("[LeadMe webhook] rejected invalid phone; keys={}", list(payload.keys()))
        raise HTTPException(status_code=422, detail="invalid phone")

    if not normalized.source_type and not normalized.campaign:
        logger.warning("[LeadMe webhook] rejected payload without source or campaign")
        raise HTTPException(status_code=422, detail="missing source or campaign")

    # Persist and process before acknowledging the event. Returning accepted
    # from a detached thread made database failures indistinguishable from a
    # successful LeadMe delivery.
    try:
        handle_new_lead(
            phone=normalized.phone,
            name=normalized.name or None,
            metadata=normalized.metadata(),
            campaign_id=normalized.campaign or None,
        )
    except Exception:
        logger.exception("[LeadMe webhook] processing failed")
        raise HTTPException(status_code=503, detail="processing failed")

    return JSONResponse(
        {"status": "accepted"},
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
