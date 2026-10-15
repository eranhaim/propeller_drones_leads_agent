"""Inbound HTTP webhook.

Purpose: LeadMe (via its "External Interfaces" mechanism) POSTs new leads
to this endpoint the moment they arrive on a campaign. We upsert the lead
in our own DB. Approved website-form sources receive one warm WhatsApp
opener via GreenAPI; other sources remain CRM-only.

Endpoint:
    POST /webhook/leadme/{secret}

Accepts either application/json or application/x-www-form-urlencoded, in
English or in LeadMe's own Hebrew field names, and normalizes casing and
array variants: phone, firstname, lastname, campaign, tags, Facebook Lead id
and sourceType.

The endpoint never rejects a delivery it understood enough to acknowledge.
LeadMe's interface fires at record intake, before the source, campaign,
status and tags are written, so a payload carrying nothing but a phone is
the normal case rather than a malformed one; the source is pulled back out
of LeadMe afterwards (``app/crm/leadme_source.py``). The only non-200 left
is 403 for a bad secret.

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
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.admin.routes import admin_session_secret, router as admin_router
from app.config import get_settings
from app.crm.leadme_client import last_known_session_status
from app.db.session import engine
from app.followup.scheduler import last_leak_canary_status, last_openai_status
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


def _database_status() -> str:
    """``"ok"`` or ``"unreachable"``.

    Bounded by the engine's ``connect_timeout``, because this runs on every
    Docker healthcheck and must answer well inside its 10s timeout.
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return "ok"
    except Exception:  # noqa: BLE001
        return "unreachable"


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
    # Per-component detail so the host-level alerter can name a cause
    # instead of reporting "the bot is unwell", and so one external check
    # also covers OpenAI billing and the CRM session, which previously
    # failed only into the Docker logs.
    #
    # None of these feed the status code. That is derived from polling
    # alone, because this endpoint is also the Docker healthcheck: a
    # drained OpenAI balance or an expired LeadMe cookie is not a reason to
    # restart the container.
    payload["openai"] = last_openai_status()
    payload["database"] = _database_status()
    payload["leadme_session"] = last_known_session_status() or "unknown"
    # Reported separately from the session because the canary has its own way
    # of going blind: a healthy session whose getPieData call starts failing
    # leaves the trash campaign unwatched while leadme_session still says
    # "healthy".
    payload["leadme_leak_canary"] = last_leak_canary_status()
    status_code = 200 if payload["status"] == "ok" else 503
    return JSONResponse(payload, status_code=status_code)


def _enqueue_lead(normalized) -> None:
    """Record the lead on a worker thread so the webhook can answer at once.

    LeadMe gives a delivery five seconds before it calls it failed, and
    ``handle_new_lead`` can spend longer than that sending the opener and the
    intro video over GreenAPI. Processing used to run inline so that a
    database failure surfaced as a failed delivery; a timeout surfaced the
    same way while also costing us the lead, so the acknowledgement comes
    first now and failures are loud in the log instead.
    """

    def _work() -> None:
        try:
            handle_new_lead(
                phone=normalized.phone,
                name=normalized.name or None,
                metadata=normalized.metadata(),
                campaign_id=normalized.campaign or None,
            )
        except Exception:
            logger.exception(
                "[LeadMe webhook] processing failed after acknowledging the "
                "delivery; the lead may not have been recorded",
            )

    Thread(target=_work, daemon=True, name="leadme-webhook-lead").start()


@app.post("/webhook/leadme/{secret}")
async def leadme_webhook(secret: str, request: Request) -> JSONResponse:
    settings = get_settings()
    expected = (settings.webhook_secret or "").strip()
    if expected and secret != expected:
        logger.warning("Rejected LeadMe webhook: bad secret ({} chars)", len(secret))
        raise HTTPException(status_code=403, detail="bad secret")

    content_type = (request.headers.get("content-type") or "").lower()
    raw_payload: Any = {}
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
    except Exception:  # noqa: BLE001
        # An unreadable body is still a delivery we must acknowledge. See
        # the status-code note below.
        logger.exception("[LeadMe webhook] could not parse the body")

    payload = _flatten_payload(raw_payload)
    normalized = normalize_leadme_payload(payload)
    logger.info("[LeadMe webhook] payload keys={}", list(payload.keys()))

    if not _is_valid_whatsapp_phone(normalized.phone):
        logger.warning(
            "[LeadMe webhook] no usable phone; nothing to record. keys={}",
            list(payload.keys()),
        )
        return JSONResponse({"status": "ignored"}, status_code=200)

    # A payload with no source or campaign used to be rejected. It is now the
    # normal case: LeadMe's interface fires at record intake, before those
    # fields are written, so demanding them rejected almost every delivery.
    # The source is pulled from LeadMe afterwards instead -- see
    # app/crm/leadme_source.py.
    _enqueue_lead(normalized)
    return JSONResponse({"status": "accepted"}, status_code=200)


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
