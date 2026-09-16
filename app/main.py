"""Entry point: run DB migrations and start the GreenAPI polling loop."""

from __future__ import annotations

import os
import sys
import threading
import time

from alembic import command
from alembic.config import Config as AlembicConfig
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from whatsapp_chatbot_python import GreenAPIBot

from app.config import get_settings
from app.db.session import engine
from app.webhook.server import (
    mark_polling_error,
    mark_polling_started,
    mark_polling_success,
    polling_seconds_since_success,
    run_in_background_thread as run_webhook,
)
from app.whatsapp.handler import register_handlers


def _configure_logging() -> None:
    settings = get_settings()
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )


def _wait_for_db(max_seconds: int = 60) -> None:
    logger.info("Waiting for database at {}", get_settings().database_url)
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Database is up")
            return
        except OperationalError as exc:
            logger.debug("DB not ready yet: {}", exc)
            time.sleep(2)
    raise RuntimeError("Database did not become ready in time")


def _run_migrations() -> None:
    logger.info("Running Alembic migrations")
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "head")
    logger.info("Migrations complete")


def _build_bot() -> GreenAPIBot:
    settings = get_settings()
    bot = GreenAPIBot(
        settings.green_api_instance_id,
        settings.green_api_token,
        host=settings.green_api_host,
        media=settings.green_api_media_host,
        # Do NOT delete queued notifications on startup. If the bot crashed
        # or was redeployed while a message was in flight, we still want to
        # process it. Missing a lead's message = lost lead.
        delete_notifications_at_startup=False,
    )
    # The upstream client otherwise allows a GreenAPI HTTP request to block
    # for three minutes. Keep the request bounded; the process watchdog below
    # is the final safeguard for a socket that fails to honour this timeout.
    bot.api.host_timeout = settings.green_api_request_timeout_seconds
    register_handlers(bot)
    return bot


def _start_polling_watchdog() -> None:
    """Restart the container if receive polling or a message handler wedges.

    GreenAPIBot processes one notification synchronously. A stuck HTTP/LLM
    call would therefore leave the HTTP health endpoint alive while every
    subsequent WhatsApp message remains queued. Exiting lets Compose's
    ``restart: unless-stopped`` recover the worker; the notification is not
    deleted until after its handler returns.
    """
    settings = get_settings()
    timeout = settings.polling_watchdog_seconds

    def _watch() -> None:
        while True:
            time.sleep(min(15, max(1, timeout // 4)))
            age = polling_seconds_since_success()
            if age is not None and age > timeout:
                logger.critical(
                    "WhatsApp poller has made no successful receive request "
                    "for {:.0f}s (limit={}s); exiting for Docker restart",
                    age,
                    timeout,
                )
                os._exit(75)

    threading.Thread(
        target=_watch, daemon=True, name="greenapi-poll-watchdog"
    ).start()


def _run_polling_loop(bot: GreenAPIBot) -> None:
    """GreenAPI polling loop with liveness instrumentation."""
    bot.api.session.headers["Connection"] = "keep-alive"
    logger.info("Bot ready -- entering polling loop")
    logger.info("Started receiving incoming notifications.")

    while True:
        try:
            mark_polling_started()
            response = bot.api.receiving.receiveNotification()
            # Empty queue responses count: they prove GreenAPI connectivity.
            mark_polling_success()

            if not response.data:
                continue

            notification = response.data
            bot.router.route_event(notification["body"])
            bot.api.receiving.deleteNotification(notification["receiptId"])
        except KeyboardInterrupt:
            break
        except Exception as exc:  # noqa: BLE001
            mark_polling_error(str(exc))
            logger.exception("GreenAPI polling/dispatch error; retrying in 5s")
            time.sleep(5)

    bot.api.session.headers["Connection"] = "close"
    logger.info("Stopped receiving incoming notifications.")


def main() -> None:
    _configure_logging()
    logger.info("Starting Propeller Drones lead-conversion bot")

    _wait_for_db()
    _run_migrations()

    run_webhook()

    from app.followup.scheduler import run_in_background_thread as _run_followup
    _run_followup()

    try:
        bot = _build_bot()
    except Exception as exc:
        logger.warning(
            "GreenAPI bot failed to start ({}). "
            "Webhook/admin UI still available -- WhatsApp polling disabled.",
            exc,
        )
        # Block forever so the webhook server (admin UI, simulator) stays up.
        import threading
        threading.Event().wait()
        return

    _start_polling_watchdog()
    _run_polling_loop(bot)


if __name__ == "__main__":
    main()
