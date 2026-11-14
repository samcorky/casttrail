import pytest

from casttrail.web.routes import duration, templates


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (None, "-"),
        (0, "0.0 s"),
        (1.25, "1.2 s"),
        (9.94, "9.9 s"),
        (10, "10 s"),
        (59.4, "59 s"),
        (59.6, "1 min 0 s"),
        (90, "1 min 30 s"),
        (3599, "59 min 59 s"),
        (3600, "1 h 0 min"),
        (4000, "1 h 6 min"),
        (90061, "25 h 1 min"),
    ],
)
def test_a_time_is_written_as_a_person_would(seconds: float | None, shown: str) -> None:
    assert duration(seconds) == shown


def test_templates_can_use_it() -> None:
    assert templates.env.filters["duration"] is duration
