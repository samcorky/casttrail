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
    device_cookie_name: str = "casttrail-device"
    device_ttl_days: int = 365
    # "auto" marks the cookie Secure when the request arrived over HTTPS.
    cookie_secure: Literal["auto", "always", "never"] = "auto"

    # Failed sign-ins tolerated within the window before further attempts are refused.
    signin_max_failures_per_user: int = 5
    signin_max_failures_per_user_known_device: int = 20
    signin_max_failures_per_ip: int = 20
    signin_window_minutes: int = 15
