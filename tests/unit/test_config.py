import pytest
from pydantic import SecretStr, ValidationError

from casttrail.config import Settings


def test_no_admin_is_configured_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.admin_email is None
    assert settings.admin_password is None


def test_admin_email_and_password_set_together_are_accepted() -> None:
    settings = Settings(
        admin_email="a@example.com", admin_password=SecretStr("s3cret-pass")
    )

    assert settings.admin_password is not None
    assert settings.admin_password.get_secret_value() == "s3cret-pass"
    assert "s3cret-pass" not in repr(settings)


@pytest.mark.parametrize(
    "values",
    [
        {"admin_email": "a@example.com"},
        {"admin_password": "s3cret-pass"},
        {"admin_email": "a@example.com", "admin_password": ""},
    ],
)
def test_half_an_admin_configuration_is_refused(values: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        Settings(**values)  # type: ignore[arg-type]
