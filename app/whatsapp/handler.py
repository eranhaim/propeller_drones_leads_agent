"""Routing for inbound WhatsApp notifications.

Wires the ``whatsapp-chatbot-python`` bot up so every incoming text
message is fed to the LangChain agent, which then replies via GreenAPI.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from whatsapp_chatbot_python import GreenAPIBot, Notification

from app.agent.graph import handle_message
from app.config import get_settings
from app.db import repository
from app.db.models import MessageRole
from app.db.session import session_scope
from app.whatsapp.sender import ChatSender


def _log_ctwa_referral(notification: Notification) -> None:
    """Temporary debug log — prints the full referral object if present."""
    event = notification.event
    referral = event.get("messageData", {}).get("referral") or event.get("referral")
    if referral:
        logger.info("[CTWA DEBUG] referral found: {}", referral)
    else:
        logger.debug("[CTWA DEBUG] no referral in event keys: {}", list(event.keys()))


def _extract_text(notification: Notification) -> Optional[str]:
    """Pull user-visible text out of any supported message type."""
    md = notification.event.get("messageData", {})
    mtype = md.get("typeMessage")

    if mtype == "textMessage":
        return md.get("textMessageData", {}).get("textMessage")
    if mtype == "extendedTextMessage":
        return md.get("extendedTextMessageData", {}).get("text")
    if mtype in {
        "imageMessage",
        "videoMessage",
        "documentMessage",
        "audioMessage",
    }:
        return md.get("fileMessageData", {}).get("caption") or ""
    if mtype == "buttonsResponseMessage":
        return md.get("buttonsResponseMessage", {}).get("selectedButtonText")
    if mtype == "listResponseMessage":
        return md.get("listResponseMessage", {}).get("title")
    return None


def _extract_sender_info(notification: Notification) -> tuple[str, Optional[str]]:
    sender_data = notification.event.get("senderData", {})
    chat_id = sender_data.get("chatId") or ""
    sender_name = sender_data.get("senderName")
    return chat_id, sender_name


def _phone_from_chat_id(chat_id: str) -> str:
    """``972501234567@c.us`` -> ``972501234567``."""
    return chat_id.split("@", 1)[0]


def _is_allowed(phone: str) -> bool:
    allowed = get_settings().allowed_test_phones
    if not allowed:
        return True
    return phone in allowed


def register_handlers(bot: GreenAPIBot) -> None:
    @bot.router.message()
    def _on_message(notification: Notification) -> None:
        _log_ctwa_referral(notification)
        chat_id, sender_name = _extract_sender_info(notification)
        if not chat_id:
            logger.debug("Notification without chatId, skipping")
            return

        if chat_id.endswith("@g.us"):
            logger.debug("Group message ignored: {}", chat_id)
            return

        phone = _phone_from_chat_id(chat_id)
        if not _is_allowed(phone):
            logger.info("Blocked phone {} (not in ALLOWED_TEST_PHONES)", phone)
            return

        text = _extract_text(notification)
        if not text:
            logger.debug("Notification with no extractable text, skipping")
            return

        # Human-takeover: if an admin muted the bot for this lead we still
        # persist the inbound message so the human sees it in the admin UI,
        # but we do NOT invoke the agent or send anything back.
        with session_scope() as session:
            lead = repository.get_or_create_lead(
                session, phone=phone, name=sender_name,
            )
            if lead.bot_muted:
                repository.add_message(session, lead, MessageRole.user, text)
                logger.info(
                    "[mute] lead {} ({}) is muted; recorded inbound msg but "
                    "skipping agent + reply.",
                    lead.id, phone,
                )
                return

        sender = ChatSender(api=notification.api, chat_id=chat_id)
        sender.send_typing()

        try:
            reply = handle_message(
                phone=phone,
                text=text,
                sender_name=sender_name,
                send_video_fn=sender.send_video,
            )
        except Exception:
            logger.exception("Failed to process message from {}", phone)
            reply = (
                "סליחה, יש לי כרגע תקלה. אנסה שוב תוך רגע - "
                "או שאפשר להשאיר טלפון ויועץ יחזור אליך."
            )

        if reply:
            sender.send_text(reply)
