import re
from pathlib import Path

import pytest

from casttrail.config import Settings

ROOT = Path(__file__).parents[2]


def _write(path: Path, *lines: str) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def env_file_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Turn ``.env`` reading back on, in an empty folder, with no real variables."""
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    monkeypatch.chdir(tmp_path)
    for name in (
        "ADMIN_EMAIL",
        "ADMIN_PASSWORD",
        "REFRESH_ENABLED",
        "REFRESH_HOUR",
        "TMDB_READ_TOKEN",
    ):
        monkeypatch.delenv(f"CASTTRAIL_{name}", raising=False)
    return tmp_path / ".env"


def test_a_dot_env_file_in_the_current_folder_is_read(env_file_on: Path) -> None:
    _write(
        env_file_on,
        "CASTTRAIL_ADMIN_EMAIL=me@example.com",
        "CASTTRAIL_ADMIN_PASSWORD=a-long-password",
        "CASTTRAIL_REFRESH_ENABLED=true",
        "CASTTRAIL_REFRESH_HOUR=7",
        "CASTTRAIL_TMDB_READ_TOKEN=tok",
    )

    settings = Settings()

    assert settings.admin_email == "me@example.com"
    assert settings.admin_password is not None
    assert settings.admin_password.get_secret_value() == "a-long-password"
    assert (settings.refresh_enabled, settings.refresh_hour) == (True, 7)
    assert settings.tmdb_read_token is not None
    assert settings.tmdb_read_token.get_secret_value() == "tok"


def test_a_real_environment_variable_beats_the_file(
    env_file_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(env_file_on, "CASTTRAIL_REFRESH_HOUR=7")
    monkeypatch.setenv("CASTTRAIL_REFRESH_HOUR", "9")

    assert Settings().refresh_hour == 9


def test_no_file_means_the_defaults(env_file_on: Path) -> None:
    assert not env_file_on.exists()

    settings = Settings()

    assert (settings.admin_email, settings.refresh_enabled) == (None, False)


def test_unrelated_lines_and_comments_in_the_file_are_ignored(
    env_file_on: Path,
) -> None:
    _write(
        env_file_on,
        "# a comment",
        "SOMETHING_ELSE=1",
        "CASTTRAIL_NOT_A_SETTING=1",
        "CASTTRAIL_REFRESH_HOUR=3",
    )

    assert Settings().refresh_hour == 3


def test_a_half_set_admin_login_in_the_file_is_refused(env_file_on: Path) -> None:
    _write(env_file_on, "CASTTRAIL_ADMIN_EMAIL=me@example.com")

    with pytest.raises(ValueError, match="set together"):
        Settings()


def test_tests_never_read_a_developers_own_dot_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write(
        tmp_path / ".env",
        "CASTTRAIL_ADMIN_EMAIL=me@example.com",
        "CASTTRAIL_ADMIN_PASSWORD=x",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CASTTRAIL_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("CASTTRAIL_ADMIN_PASSWORD", raising=False)

    assert Settings().admin_email is None


def test_the_real_default_reads_dot_env() -> None:
    """The class itself asks for ``.env``; only the test suite switches it off."""
    source = (ROOT / "src" / "casttrail" / "config.py").read_text(encoding="utf-8")

    assert 'env_file=".env"' in source


def test_dot_env_is_ignored_by_git_and_the_example_is_not() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in ignored
    assert ".env.example" not in ignored


def test_the_example_file_names_only_settings_that_exist() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    named = set(re.findall(r"^#?\s*CASTTRAIL_([A-Z_]+)=", example, flags=re.MULTILINE))
    wanted = {name.upper() for name in Settings.model_fields}

    assert named <= wanted
    assert {"ADMIN_EMAIL", "TMDB_READ_TOKEN", "REFRESH_ENABLED"} <= named


def test_the_example_file_loads_as_it_stands(env_file_on: Path) -> None:
    env_file_on.write_text(
        (ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )

    settings = Settings()

    password = settings.admin_password
    assert not settings.admin_email
    assert password is None or not password.get_secret_value()
    assert settings.refresh_enabled is False
