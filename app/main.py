"""Entry point: run DB migrations and start the GreenAPI polling loop."""

from __future__ import annotations

import sys
import time

from alembic import command
from alembic.config import Config as AlembicConfig
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from whatsapp_chatbot_python import GreenAPIBot

from app.config import get_settings
from app.db.session import engine
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
        # Do NOT delete queued notifications on startup. If the bot crashed
        # or was redeployed while a message was in flight, we still want to
        # process it. Missing a lead's message = lost lead.
        delete_notifications_at_startup=False,
    )
    register_handlers(bot)
    return bot


def main() -> None:
    _configure_logging()
    logger.info("Starting Propeller Drones lead-conversion bot")

    _wait_for_db()
    _run_migrations()

    from app.webhook.server import run_in_background_thread as _run_webhook
    _run_webhook()

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

    logger.info("Bot ready -- entering polling loop")
    bot.run_forever()


if __name__ == "__main__":
    main()
