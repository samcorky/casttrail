from importlib.metadata import PackageNotFoundError
from typing import TYPE_CHECKING

import casttrail

if TYPE_CHECKING:
    import pytest


def test_get_version_returns_a_non_empty_string() -> None:
    assert casttrail.get_version()


def test_get_version_falls_back_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(casttrail, "version", missing)

    assert casttrail.get_version() == "0+unknown"
