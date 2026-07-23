"""Routing for inbound WhatsApp notifications.

Wires the ``whatsapp-chatbot-python`` bot up so every incoming text
message is fed to the LangChain agent, which then replies via GreenAPI.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from whatsapp_chatbot_python import GreenAPIBot, Notification

import threading

from app.agent.graph import handle_message
from app.config import get_settings
from app.db import repository
from app.db.models import MessageRole
from app.db.session import session_scope
from app.whatsapp.sender import ChatSender


def _detect_ctwa_campaign(text: str) -> Optional[str]:
    """Detect which CTWA campaign sent this opening message.

    Each campaign uses a distinct greeting text set in Meta Ads Manager.
    We match on unique keywords rather than exact strings so minor edits
    to the ad copy don't break attribution.

    Returns a campaign label (e.g. "עודד") or None if not a CTWA message.
    """
    t = text.strip()
    if "מאסטר" in t:
        return "מאסטר"
    if "הכשרה" in t:
        return "רוי"
    if t.startswith("מעוניין"):
        return "טל"
    if "אשמח לקבל פרטים על קורס" in t:
        return "עודד"
    return None


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


def _push_ctwa_tag(phone: str, campaign: str) -> None:
    """Push a campaign attribution tag to LeadMe (runs in background thread)."""
    try:
        from app.crm.leadme_delete import _build_client, get_row_by_phone
        from app.crm.leadme_client import _resolve_tag_lead_id, _admin_add_tag
        import time

        client = _build_client()
        if client is None:
            logger.warning("[CTWA] no admin cookies, cannot push tag for {}", phone)
            return

        row = None
        for attempt in range(4):
            row = get_row_by_phone(phone, client)
            if row is not None:
                break
            if attempt < 3:
                time.sleep((attempt + 1) * 5)

        if row is None:
            logger.warning("[CTWA] phone {} not found in LeadMe, skipping tag", phone)
            return

        lc_id = str(row[1]).strip() if len(row) > 1 else ""
        if not lc_id or not lc_id.isdigit():
            logger.warning("[CTWA] no numeric id for {}", phone)
            return

        tag_lead_id = _resolve_tag_lead_id(client, lc_id)
        tag = f"מקור: {campaign}"
        ok = _admin_add_tag(client, tag_lead_id, tag)
        logger.info("[CTWA] tag {!r} pushed for {} ok={}", tag, phone, ok)
    except Exception:
        logger.exception("[CTWA] failed to push tag for {}", phone)


def register_handlers(bot: GreenAPIBot) -> None:
    @bot.router.message()
    def _on_message(notification: Notification) -> None:
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

            # CTWA attribution: tag the lead with their campaign on first message.
            campaign = _detect_ctwa_campaign(text)
            if campaign:
                existing = (lead.lead_metadata or {}).get("ctwa_campaign")
                if not existing:
                    repository.update_lead_metadata(session, lead, ctwa_campaign=campaign)
                    logger.info("[CTWA] lead {} attributed to campaign={!r}", phone, campaign)
                    threading.Thread(
                        target=_push_ctwa_tag,
                        args=(phone, campaign),
                        daemon=True,
                    ).start()

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
