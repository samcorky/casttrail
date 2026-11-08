from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from ``CASTTRAIL_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="CASTTRAIL_", extra="ignore")

    # TMDB's "API Read Access Token". Without it, search says it is not configured.
    tmdb_read_token: SecretStr | None = None

    # The first administrator, created only when the database has no users at all.
    # There are no default credentials: with neither set, nobody is created.
    admin_email: str | None = None
    admin_password: SecretStr | None = None
    admin_display_name: str = "Admin"

    # The interactive API docs are off unless explicitly enabled.
    docs_enabled: bool = False
    database_url: str = "sqlite+aiosqlite:///data/casttrail.db"

    # Off by default, so nothing contacts Rostercon until you ask for it. When on, the
    # refresh for watched people runs once a day at this hour (server local time).
    # The admin page can run it by hand either way.
    refresh_enabled: bool = False
    refresh_hour: int = Field(default=4, ge=0, le=23)

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

    @model_validator(mode="after")
    def _admin_credentials_come_as_a_pair(self) -> Self:
        if bool(self.admin_email) != bool(
            self.admin_password and self.admin_password.get_secret_value()
        ):
            msg = "CASTTRAIL_ADMIN_EMAIL and _PASSWORD must be set together"
            raise ValueError(msg)
        return self
