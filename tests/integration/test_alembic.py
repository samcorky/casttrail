"""pytest-alembic's standard migration checks, run against a throwaway SQLite file."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy
from alembic.config import Config
from pytest_alembic.tests import (
    test_model_definitions_match_ddl,  # noqa: F401
    test_single_head_revision,  # noqa: F401
    test_up_down_consistency,  # noqa: F401
    test_upgrade,  # noqa: F401
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


@pytest.fixture
def alembic_config(database_url: str) -> Config:
    """Alembic config pointing at the test database."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def alembic_engine(database_url: str) -> Iterator[sqlalchemy.Engine]:
    """A sync engine over the same file, as pytest-alembic runs synchronously."""
    engine = sqlalchemy.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        yield engine
    finally:
        engine.dispose()
