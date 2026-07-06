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

    # LeadMe CRM (all optional -- if url is empty the client no-ops and just logs)
    leadme_api_url: str = Field("", alias="LEADME_API_URL")
    leadme_api_token: str = Field("", alias="LEADME_API_TOKEN")
    leadme_auth_scheme: str = Field("Bearer", alias="LEADME_AUTH_SCHEME")  # Bearer | Token | Basic | Query | None
    leadme_auth_query_key: str = Field("api_key", alias="LEADME_AUTH_QUERY_KEY")
    leadme_ready_status: str = Field("ready_for_call", alias="LEADME_READY_STATUS")
    leadme_source_label: str = Field("WhatsApp Bot", alias="LEADME_SOURCE_LABEL")
    # JSON mapping of internal field -> LeadMe field name.
    # Example: {"phone":"phone","name":"full_name","status":"status","note":"comments"}
    leadme_field_map_json: str = Field("", alias="LEADME_FIELD_MAP")

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
