"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # OpenAI
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_chat_model: str = Field("gpt-4o", alias="OPENAI_CHAT_MODEL")
    openai_embedding_model: str = Field(
        "text-embedding-3-small", alias="OPENAI_EMBEDDING_MODEL"
    )

    # GreenAPI
    green_api_instance_id: str = Field(..., alias="GREEN_API_INSTANCE_ID")
    green_api_token: str = Field(..., alias="GREEN_API_TOKEN")
    green_api_host: str = Field("https://api.green-api.com", alias="GREEN_API_HOST")
    green_api_media_host: str = Field(
        "https://media.green-api.com", alias="GREEN_API_MEDIA_HOST"
    )

    # Database
    database_url: str = Field(
        "postgresql+psycopg2://propeller:propeller@postgres:5432/propeller_bot",
        alias="DATABASE_URL",
    )

    # Chroma
    chroma_host: str = Field("chroma", alias="CHROMA_HOST")
    chroma_port: int = Field(8000, alias="CHROMA_PORT")
    chroma_collection: str = Field("propeller_knowledge", alias="CHROMA_COLLECTION")

    # Knowledge sources
    propeller_website_base: str = Field(
        "https://propeller-drones.com", alias="PROPELLER_WEBSITE_BASE"
    )

    # Access control -- if empty, allow anyone
    allowed_test_phones_raw: str = Field("", alias="ALLOWED_TEST_PHONES")

    # Inbound webhook (LeadMe -> our bot: fresh lead arrival)
    webhook_port: int = Field(8080, alias="WEBHOOK_PORT")
    # Path segment secret. LeadMe hits /webhook/leadme/{webhook_secret}
    # Empty => any request accepted (dev-mode only, do NOT run in prod).
    webhook_secret: str = Field("", alias="WEBHOOK_SECRET")

    # LeadMe CRM - public "supplier" API
    # If LEADME_INSERT_URL is empty the client no-ops and just logs.
    # Provisioned in LeadMe under Preferences -> Suppliers -> {supplier} -> API.
    leadme_insert_url: str = Field("", alias="LEADME_INSERT_URL")
    leadme_update_url: str = Field("", alias="LEADME_UPDATE_URL")
    leadme_status_id: str = Field("", alias="LEADME_STATUS_ID")
    leadme_source_label: str = Field("WhatsApp Bot", alias="LEADME_SOURCE_LABEL")

    # LeadMe's PUBLIC supplier API can only INSERT and UPDATE, not delete. To
    # let the admin panel wipe a lead from LeadMe as well (used for manual
    # QA -- reset a phone and re-submit the form), we fall back to LeadMe's
    # INTERNAL admin endpoints via httpx with saved session cookies +
    # CodeIgniter CSRF token. Both files are exported once from a logged-in
    # browser and mounted into the container.
    #
    # - leadme_cookies_path: JSON file exported from Chrome/Playwright with
    #   the LeadMe session cookies (PHPSESSID + csrf_cookie_name).
    #   Empty string => admin delete only wipes our local DB, not LeadMe.
    # - leadme_admin_base: base URL of the admin app (as opposed to the
    #   /supplier public API).
    leadme_cookies_path: str = Field(
        "data/leadme_cookies.json", alias="LEADME_COOKIES_PATH",
    )
    leadme_admin_base: str = Field(
        "https://www.leadmecms.co.il", alias="LEADME_ADMIN_BASE",
    )
    # When true, ALL LeadMe writes (insert/update/cancel) become no-ops that
    # only log. Read-side (search/delete) still works. Used by the eval
    # harness so fake 999xxx phones don't pollute LeadMe.
    leadme_test_mode: bool = Field(False, alias="LEADME_TEST_MODE")

    # How to talk to LeadMe when we have new info about a lead:
    #   - "update-only": call /supplier/update/p/{slug} to modify the
    #     existing LeadMe lead's status/tags. Do NOT call /supplier/insert.
    #     This is the default because ~all our leads originate from
    #     LeadMe's own webhook (customer's website form -> LeadMe -> us),
    #     so a supplier-insert creates a DUPLICATE that lands in whatever
    #     campaign the supplier slug is currently mapped to -- which the
    #     customer says has been the "removed from WhatsApp" trash
    #     campaign. Update-only avoids duplicates entirely.
    #   - "insert-then-update": legacy behavior. Kept for edge cases where
    #     a lead never came through the webhook.
    #   - "never": no LeadMe writes at all (useful for read-only staging).
    leadme_insert_mode: str = Field(
        "update-only", alias="LEADME_INSERT_MODE",
    )

    # Status IDs for the 3 engagement levels the customer asked for.
    # Level 1: booked a call with the bot.
    # Level 2: replied to the bot but never booked.
    # Level 3: never replied to the bot (opener only).
    # Empty string => skip the status update for that level, but still
    # push the engagement TAG so the sales team can filter in LeadMe.
    leadme_status_level_1: str = Field("", alias="LEADME_STATUS_LEVEL_1")
    leadme_status_level_2: str = Field("", alias="LEADME_STATUS_LEVEL_2")
    leadme_status_level_3: str = Field("", alias="LEADME_STATUS_LEVEL_3")

    # Admin UI (HTTP Basic auth for /admin routes)
    admin_user: str = Field("", alias="ADMIN_USER")
    admin_password: str = Field("", alias="ADMIN_PASSWORD")

    # Follow-up nudges (wake-up messages for silent leads)
    followup_enabled: bool = Field(True, alias="FOLLOWUP_ENABLED")
    followup_interval_minutes: int = Field(30, alias="FOLLOWUP_INTERVAL_MINUTES")
    followup_first_hours: int = Field(24, alias="FOLLOWUP_FIRST_HOURS")
    followup_second_hours: int = Field(48, alias="FOLLOWUP_SECOND_HOURS")
    followup_max_nudges: int = Field(2, alias="FOLLOWUP_MAX_NUDGES")
    # Polite window in Asia/Jerusalem local time -- inclusive start, exclusive end.
    followup_quiet_start_hour: int = Field(9, alias="FOLLOWUP_QUIET_START_HOUR")
    followup_quiet_end_hour: int = Field(20, alias="FOLLOWUP_QUIET_END_HOUR")

    # Webinar-specific follow-up (a separate nudge after the 55-min webinar
    # was sent; asks "did you watch?" rather than the generic silence nudge).
    webinar_followup_hours: int = Field(6, alias="WEBINAR_FOLLOWUP_HOURS")
    # Video-specific follow-up (after a non-webinar video is sent).
    video_followup_hours: int = Field(2, alias="VIDEO_FOLLOWUP_HOURS")

    # LeadMe push queue -- see app/crm/leadme_queue.py. How often to
    # drain pending pushes for leads whose phone wasn't yet visible in
    # LeadMe at the time of the request. Small enough to keep the CTWA
    # race under 5 minutes typical, large enough to not hammer LeadMe.
    leadme_queue_interval_minutes: int = Field(
        3, alias="LEADME_QUEUE_INTERVAL_MINUTES",
    )

    # LeadMe automatic cookie refresh -- see app/crm/leadme_login.py.
    # LeadMe has no admin API (as of 2026-07); we impersonate a
    # logged-in browser via saved cookies. The CSRF cookie
    # hard-expires every 24h, so cookies must be rotated regularly.
    # These vars power a pure-httpx + 2Captcha refresh flow that
    # replaces the manual "paste cookies from DevTools" step:
    #
    # - leadme_auto_refresh_enabled: master switch. False -> no-op.
    # - leadme_login_email / _password: the LeadMe account we log in
    #   with. Should be a dedicated bot account, NOT a human's.
    # - leadme_captcha_api_key: 2Captcha API key (or compatible
    #   provider). Cost is <$0.01 per solve; expect ~2-3 solves/day.
    # - leadme_auto_refresh_interval_hours: proactive refresh cadence.
    #   Should be < 24 (the csrf_cookie_name hard-expiry).
    leadme_auto_refresh_enabled: bool = Field(
        False, alias="LEADME_AUTO_REFRESH_ENABLED",
    )
    leadme_login_email: str = Field("", alias="LEADME_LOGIN_EMAIL")
    leadme_login_password: str = Field("", alias="LEADME_LOGIN_PASSWORD")
    leadme_captcha_api_key: str = Field("", alias="LEADME_CAPTCHA_API_KEY")
    leadme_auto_refresh_interval_hours: int = Field(
        12, alias="LEADME_AUTO_REFRESH_INTERVAL_HOURS",
    )

    # Shopify Storefront API (read-only: product search, prices, inventory)
    shopify_storefront_token: str = Field("", alias="SHOPIFY_STOREFRONT_TOKEN")
    shopify_storefront_url: str = Field("", alias="SHOPIFY_STOREFRONT_URL")

    # Logging
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @property
    def allowed_test_phones(self) -> List[str]:
        return [
            p.strip()
            for p in self.allowed_test_phones_raw.split(",")
            if p.strip()
        ]

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()


@lru_cache
def get_settings() -> Settings:
    return Settings()
