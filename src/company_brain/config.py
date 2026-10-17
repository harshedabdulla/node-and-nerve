from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_username: str = ""
    falkordb_password: SecretStr = SecretStr("")
    falkordb_ssl: bool = False  # FalkorDB Cloud instances with TLS enabled
    falkordb_query_timeout_ms: int = Field(default=5000, ge=100, le=30000)
    falkordb_socket_timeout_s: float = Field(default=10.0, gt=0)
    falkordb_connect_timeout_s: float = Field(default=3.0, gt=0)

    # Comma-separated "key=company1|company2" entries. "key=*" grants every company.
    # Empty disables authentication (local development only).
    company_brain_api_keys: SecretStr = SecretStr("")

    # Optional AI agent (OpenRouter, OpenAI-compatible) and Telegram bot. The engine
    # itself never needs these.
    open_router_api_key: SecretStr = SecretStr("")
    openrouter_model: str = "anthropic/claude-sonnet-5.5"
    telegram_bot_token: SecretStr = SecretStr("")
    # Comma-separated Telegram chat ids allowed to talk to the bot. Empty = nobody.
    telegram_allowed_chat_ids: str = ""
    # Webhook mode (serverless hosting): Telegram sends this in X-Telegram-Bot-Api-Secret-Token.
    # Empty disables POST /telegram/webhook.
    telegram_webhook_secret: SecretStr = SecretStr("")
    agent_company_id: str = "kaveri-body-works"
    # Trace links: where the API is reachable from the reader's browser, and the secret
    # used to sign links (they open without an API key, so they are signed and expire).
    public_base_url: str = "http://localhost:8000"
    trace_link_secret: SecretStr = SecretStr("")


@lru_cache
def get_settings() -> Settings:
    return Settings()
