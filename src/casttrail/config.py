from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from ``CASTTRAIL_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="CASTTRAIL_", extra="ignore")

    # The interactive API docs are off unless explicitly enabled.
    docs_enabled: bool = False
    database_url: str = "sqlite+aiosqlite:///data/casttrail.db"
