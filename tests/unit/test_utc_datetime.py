from datetime import UTC, datetime, timedelta, timezone

import pytest

from casttrail.db import UtcDateTime

TYPE = UtcDateTime()


def test_none_passes_through_both_ways() -> None:
    assert TYPE.process_bind_param(None, None) is None  # type: ignore[arg-type]
    assert TYPE.process_result_value(None, None) is None  # type: ignore[arg-type]


def test_a_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TYPE.process_bind_param(datetime(2026, 1, 1), None)  # type: ignore[arg-type]


def test_an_aware_datetime_is_stored_as_naive_utc_and_read_back_aware() -> None:
    plus_two = timezone(timedelta(hours=2))
    moment = datetime(2026, 1, 1, 14, 0, tzinfo=plus_two)

    stored = TYPE.process_bind_param(moment, None)  # type: ignore[arg-type]
    loaded = TYPE.process_result_value(stored, None)  # type: ignore[arg-type]

    assert stored == datetime(2026, 1, 1, 12, 0)
    assert loaded == moment
    assert loaded is not None
    assert loaded.tzinfo is UTC
