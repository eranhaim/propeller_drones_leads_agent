"""Outbound WhatsApp actions bound to a specific chat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from app.videos.catalog import Video


@dataclass
class ChatSender:
    """Send text and files to a single WhatsApp chat via GreenAPI.

    ``chat_id`` is the WhatsApp id, e.g. ``972501234567@c.us``.
    ``api`` is a ``whatsapp_api_client_python.API.GreenAPI`` instance
    (typically ``notification.api`` inside a chatbot handler).
    """

    api: object
    chat_id: str

    def send_text(self, text: str) -> None:
        if not text:
            return
        try:
            self.api.sending.sendMessage(self.chat_id, text)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Failed to send text to {}", self.chat_id)
            raise

    def send_video(self, video: Video, caption: Optional[str] = None) -> None:
        """Send a piece of media from the catalog.

        For ``kind="file"`` -- fetches the URL as a native WhatsApp video.
        For ``kind="link"`` -- sends a text message with the URL so WhatsApp
        renders a link preview (used for YouTube / vimeo / etc that can't be
        streamed as native video).
        """
        try:
            if video.kind == "link":
                text_caption = caption or video.title
                message = f"{text_caption}\n{video.url}"
                self.api.sending.sendMessage(self.chat_id, message)  # type: ignore[attr-defined]
                return

            file_name = f"{video.id}.mp4"
            self.api.sending.sendFileByUrl(  # type: ignore[attr-defined]
                self.chat_id,
                video.url,
                file_name,
                caption or video.title,
            )
        except Exception:
            logger.exception(
                "Failed to send media {} (kind={}) to {}",
                video.id, video.kind, self.chat_id,
            )
            raise

    def send_typing(self) -> None:
        """Best-effort typing indicator (not all GreenAPI plans support this)."""
        try:
            if hasattr(self.api.sending, "showMessagePreview"):  # type: ignore[attr-defined]
                self.api.sending.showMessagePreview(self.chat_id)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
