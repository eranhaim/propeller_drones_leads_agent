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

    # Follow-up nudges (wake-up messages for silent leads)
    followup_enabled: bool = Field(True, alias="FOLLOWUP_ENABLED")
    followup_interval_minutes: int = Field(30, alias="FOLLOWUP_INTERVAL_MINUTES")
    followup_first_hours: int = Field(24, alias="FOLLOWUP_FIRST_HOURS")
    followup_second_hours: int = Field(72, alias="FOLLOWUP_SECOND_HOURS")
    followup_max_nudges: int = Field(2, alias="FOLLOWUP_MAX_NUDGES")
    # Polite window in Asia/Jerusalem local time -- inclusive start, exclusive end.
    followup_quiet_start_hour: int = Field(9, alias="FOLLOWUP_QUIET_START_HOUR")
    followup_quiet_end_hour: int = Field(20, alias="FOLLOWUP_QUIET_END_HOUR")

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
