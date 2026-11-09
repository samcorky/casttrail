import io
import json
import logging
from typing import TYPE_CHECKING, Any

import pytest
import structlog
import structlog.testing
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from casttrail.logging_config import REDACTED, configure_logging, redact, use_json
from casttrail.main import create_app
from casttrail.web.request_log import RequestLogMiddleware

if TYPE_CHECKING:
    from collections.abc import Iterator, MutableMapping

    from starlette.requests import Request
    from starlette.responses import Response

    from casttrail.config import Settings


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    yield
    configure_logging("INFO", "auto")
    structlog.contextvars.clear_contextvars()


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_json_is_chosen_when_nobody_is_watching_and_console_when_they_are() -> None:
    assert use_json("auto", tty=False) is True
    assert use_json("auto", tty=True) is False


def test_a_format_can_be_forced_either_way() -> None:
    assert use_json("json", tty=True) is True
    assert use_json("console", tty=False) is False


def test_an_event_comes_out_as_json_with_its_values_and_a_level() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)

    structlog.get_logger("casttrail.demo").info("thing_done", count=3)

    [line] = _lines(stream)
    assert line["event"] == "thing_done"
    assert line["count"] == 3
    assert line["level"] == "info"
    assert line["logger"] == "casttrail.demo"
    assert "timestamp" in line


def test_the_console_format_is_not_json() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "console", stream=stream)

    structlog.get_logger("casttrail.demo").info("thing_done", count=3)

    text = stream.getvalue()
    assert "thing_done" in text
    assert not text.lstrip().startswith("{")


def test_the_level_hides_what_is_less_severe() -> None:
    stream = io.StringIO()
    configure_logging("WARNING", "json", stream=stream)
    log = structlog.get_logger("casttrail.demo")

    log.info("hidden")
    log.warning("shown")

    assert [line["event"] for line in _lines(stream)] == ["shown"]


def test_an_unknown_level_falls_back_to_info() -> None:
    stream = io.StringIO()
    configure_logging("LOUD", "json", stream=stream)

    structlog.get_logger("casttrail.demo").debug("hidden")
    structlog.get_logger("casttrail.demo").info("shown")

    assert [line["event"] for line in _lines(stream)] == ["shown"]


@pytest.mark.parametrize(
    "key",
    ["password", "new_password", "TMDB_TOKEN", "Authorization", "session_cookie"],
)
def test_values_under_secret_looking_names_are_blanked(key: str) -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)

    structlog.get_logger("casttrail.demo").info("x", **{key: "hunter2", "ok": "fine"})

    [line] = _lines(stream)
    assert line[key] == REDACTED
    assert line["ok"] == "fine"
    assert "hunter2" not in stream.getvalue()


def test_redact_leaves_ordinary_keys_alone() -> None:
    event = {"event": "x", "path": "/a", "status": 200}

    assert redact(None, "info", dict(event)) == event


def test_the_standard_librarys_logging_gets_the_same_format() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)

    logging.getLogger("alembic.demo").info("Running upgrade")

    [line] = _lines(stream)
    assert line["event"] == "Running upgrade"
    assert line["level"] == "info"


def test_chatty_libraries_are_kept_to_warnings() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", "json", stream=stream)

    logging.getLogger("httpcore").debug("noise")
    logging.getLogger("sqlalchemy.engine").info("SELECT 1")

    assert stream.getvalue() == ""


def test_configuring_twice_does_not_write_each_line_twice() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    configure_logging("INFO", "json", stream=stream)

    structlog.get_logger("casttrail.demo").info("once")

    assert len(_lines(stream)) == 1


def test_bound_context_appears_on_every_line_until_cleared() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    structlog.contextvars.bind_contextvars(request_id="abc123")

    structlog.get_logger("casttrail.demo").info("inside")
    structlog.contextvars.clear_contextvars()
    structlog.get_logger("casttrail.demo").info("outside")

    inside, outside = _lines(stream)
    assert inside["request_id"] == "abc123"
    assert "request_id" not in outside


# ---- the request line -------------------------------------------------------------


def _request_events(
    logs: list[MutableMapping[str, Any]],
) -> list[MutableMapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == "request"]


def test_a_request_is_logged_with_method_path_status_and_time(
    settings: Settings,
) -> None:
    with (
        TestClient(create_app(settings)) as client,
        structlog.testing.capture_logs() as logs,
    ):
        client.get("/sign-in")

    [entry] = _request_events(logs)
    assert entry["method"] == "GET"
    assert entry["path"] == "/sign-in"
    assert isinstance(entry["status"], int)
    assert isinstance(entry["duration_ms"], float)


def test_the_query_string_is_never_logged(settings: Settings) -> None:
    with (
        TestClient(create_app(settings)) as client,
        structlog.testing.capture_logs() as logs,
    ):
        client.get("/sign-in?q=private-search&token=abc")

    assert "private-search" not in repr(logs)
    assert "token=abc" not in repr(logs)


def test_every_answer_carries_a_request_id(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        first = client.get("/health").headers["X-Request-ID"]
        second = client.get("/health").headers["X-Request-ID"]

    assert first != second
    assert len(first) == 12


def test_a_callers_short_request_id_is_kept(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health", headers={"X-Request-ID": "trace42"})

    assert response.headers["X-Request-ID"] == "trace42"


@pytest.mark.parametrize("given", ["has space", "x" * 200, "semi;colon", "<b>"])
def test_an_odd_request_id_is_replaced(settings: Settings, given: str) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health", headers={"X-Request-ID": given})

    assert response.headers["X-Request-ID"] != given


def test_health_and_static_checks_are_quiet_unless_they_fail(
    settings: Settings,
) -> None:
    with (
        TestClient(create_app(settings)) as client,
        structlog.testing.capture_logs() as logs,
    ):
        client.get("/health")

    [entry] = _request_events(logs)
    assert entry["log_level"] == "debug"


def test_a_missing_page_is_logged_at_info_with_its_status(settings: Settings) -> None:
    with (
        TestClient(create_app(settings)) as client,
        structlog.testing.capture_logs() as logs,
    ):
        client.get("/no-such-page")

    [entry] = _request_events(logs)
    assert entry["status"] == 404
    assert entry["log_level"] == "info"


def test_a_failing_health_check_is_not_hidden(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    with (
        TestClient(create_app(settings)) as client,
        structlog.testing.capture_logs() as logs,
    ):
        monkeypatch.setattr(AsyncSession, "execute", broken)
        client.get("/health")

    [entry] = _request_events(logs)
    assert entry["status"] == 503
    assert entry["log_level"] == "error"


def test_an_error_nothing_else_caught_is_logged_with_its_traceback() -> None:
    async def boom(_request: Request) -> Response:
        raise RuntimeError("boom")

    app = Starlette(routes=[Route("/boom", boom)])
    app.add_middleware(RequestLogMiddleware)

    with (
        TestClient(app, raise_server_exceptions=False) as client,
        structlog.testing.capture_logs() as logs,
    ):
        client.get("/boom?secret=1")

    [entry] = [e for e in logs if e["event"] == "request_failed"]
    assert entry["path"] == "/boom"
    assert entry["exc_info"] is True
    assert "secret=1" not in repr(logs)
