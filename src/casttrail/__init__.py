"""CastTrail: convention guest tracker and autograph collection manager."""

from importlib.metadata import PackageNotFoundError, version


def get_version() -> str:
    """Return the installed package version.

    Returns:
        The distribution version, or ``"0+unknown"`` when not installed.
    """
    try:
        return version("casttrail")
    except PackageNotFoundError:
        return "0+unknown"
