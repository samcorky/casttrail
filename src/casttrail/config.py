from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from ``CASTTRAIL_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="CASTTRAIL_", extra="ignore")

    # The interactive API docs are off unless explicitly enabled.
    docs_enabled: bool = False
    database_url: str = "sqlite+aiosqlite:///data/casttrail.db"

    session_ttl_hours: int = 24 * 30
    cookie_name: str = "casttrail-session"
    # "auto" marks the cookie Secure when the request arrived over HTTPS.
    cookie_secure: Literal["auto", "always", "never"] = "auto"
