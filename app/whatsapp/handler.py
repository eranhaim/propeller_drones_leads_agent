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


def _push_ctwa_tag(lead_id: int, phone: str, campaign: str) -> None:
    """Push a campaign attribution tag to LeadMe (best-effort, single try).

    Runs in a background thread on FIRST inbound (see caller). If the
    LeadMe row can't be resolved on this one-shot attempt (CTWA race:
    Facebook lead reached us before LeadMe's supplier sync landed) we
    enqueue the tag in the persistent LeadMe queue and let the
    scheduler drain it -- see :mod:`app.crm.leadme_queue`. Cross-restart
    safe.
    """
    try:
        from app.crm.leadme_client import (
            _admin_add_tag,
            _resolve_tag_lead_id,
            BANNED_LEAKY_CAMPAIGN_ID,
            BANNED_LEAKY_CAMPAIGN_NAME,
        )
        from app.crm.leadme_delete import _build_client, get_row_by_phone
        from app.crm import leadme_queue
        from app.db.models import Lead as _Lead
        from app.db.session import session_scope
        import re as _re

        client = _build_client()
        if client is None:
            logger.warning(
                "[CTWA] no admin cookies for {} -- enqueueing for retry",
                phone,
            )
            with session_scope() as sess:
                lead = sess.get(_Lead, lead_id)
                if lead is not None:
                    leadme_queue.enqueue_ctwa_tag(
                        lead, campaign, session=sess,
                    )
            return

        try:
            row = get_row_by_phone(phone, client)
        finally:
            # Close early -- we might re-open in the queue path.
            pass

        if row is None:
            logger.info(
                "[CTWA] phone {} not visible in LeadMe yet -- enqueueing "
                "campaign={!r} for background retry", phone, campaign,
            )
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            with session_scope() as sess:
                lead = sess.get(_Lead, lead_id)
                if lead is not None:
                    leadme_queue.enqueue_ctwa_tag(
                        lead, campaign, session=sess,
                    )
            return

        lc_id = str(row[1]).strip() if len(row) > 1 else ""
        if not lc_id or not lc_id.isdigit():
            logger.warning("[CTWA] no numeric id for {}", phone)
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            return

        # Refuse to tag a lead sitting in the banned trash campaign.
        campaign_name = ""
        if len(row) > 4 and isinstance(row[4], str):
            campaign_name = _re.sub(r"<[^>]+>", " ", row[4])
            campaign_name = _re.sub(r"\s+", " ", campaign_name).strip()
        if (BANNED_LEAKY_CAMPAIGN_ID in (row[0] or "")
                or campaign_name == BANNED_LEAKY_CAMPAIGN_NAME):
            logger.error(
                "[CTWA SAFETY] refusing to tag {} -- sits in banned "
                "campaign {!r}", phone,
                campaign_name or BANNED_LEAKY_CAMPAIGN_NAME,
            )
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            return

        tag_lead_id = _resolve_tag_lead_id(client, lc_id)
        if tag_lead_id is None:
            # viewLead returned login page -- queue and let the drain
            # retry with a fresh client.
            logger.warning(
                "[CTWA] could not resolve internal leadId for lc_id={} "
                "phone={} -- enqueueing", lc_id, phone,
            )
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            with session_scope() as sess:
                lead = sess.get(_Lead, lead_id)
                if lead is not None:
                    leadme_queue.enqueue_ctwa_tag(
                        lead, campaign, session=sess,
                    )
            return

        tag = f"מקור: {campaign}"
        ok = _admin_add_tag(client, tag_lead_id, tag)
        logger.info("[CTWA] tag {!r} pushed for {} ok={}", tag, phone, ok)
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        # Even a 200/result:true can be spurious in edge cases; if the
        # admin log came back False, queue for a proper retry.
        if not ok:
            with session_scope() as sess:
                lead = sess.get(_Lead, lead_id)
                if lead is not None:
                    leadme_queue.enqueue_ctwa_tag(
                        lead, campaign, session=sess,
                    )
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
                    _lead_id_for_thread = lead.id
                    threading.Thread(
                        target=_push_ctwa_tag,
                        args=(_lead_id_for_thread, phone, campaign),
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
