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
    falkordb_query_timeout_ms: int = Field(default=5000, ge=100, le=30000)
    falkordb_socket_timeout_s: float = Field(default=10.0, gt=0)
    falkordb_connect_timeout_s: float = Field(default=3.0, gt=0)

    # Comma-separated "key=company1|company2" entries. "key=*" grants every company.
    # Empty disables authentication (local development only).
    company_brain_api_keys: SecretStr = SecretStr("")


@lru_cache
def get_settings() -> Settings:
    return Settings()
