from typing import TYPE_CHECKING

import pytest

from casttrail.config import Settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _ignore_the_local_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's real ``.env`` out of every test.

    Tests that check the file is read turn it back on for themselves.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    """A throwaway SQLite database URL inside the test's temp directory."""
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def settings(database_url: str) -> Settings:
    """Settings that never touch the real ``data/`` directory."""
    return Settings(database_url=database_url, backups_enabled=False)
