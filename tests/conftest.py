from typing import TYPE_CHECKING

import pytest

from casttrail.config import Settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    """A throwaway SQLite database URL inside the test's temp directory."""
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def settings(database_url: str) -> Settings:
    """Settings that never touch the real ``data/`` directory."""
    return Settings(database_url=database_url)
