"""Logging for the whole app, through structlog.

Every log line is an *event name* plus key-value pairs (``refresh_finished
people=4 events_fetched=11``), never a sentence with values pasted into it, so lines
can be searched and counted. Code uses ``structlog.get_logger(__name__)``; the standard
library's own ``logging`` (used by Alembic, APScheduler, Granian and others) is routed
through the same formatting, so there is one stream and one look.

Output is JSON when nothing is watching the terminal (a container, a file) and a
coloured console otherwise. Anything whose name looks secret is blanked on the way out.
"""

import logging
import sys
from typing import TYPE_CHECKING, Literal, TextIO

import structlog

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger

LogFormat = Literal["auto", "console", "json"]
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
REDACTED = "[redacted]"
# A key containing any of these is blanked, wherever it appears and however it is cased.
SECRET_WORDS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "private_key",
)

# Libraries that log a lot at INFO about things we log better ourselves, or that put
# addresses in their messages.
_QUIET = {
    "httpx2": logging.WARNING,
    "httpcore": logging.WARNING,
    "aiosqlite": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "apscheduler": logging.WARNING,
    "multipart": logging.WARNING,
    "asyncio": logging.WARNING,
}
_OWNED = "_casttrail_handler"


class _CurrentStdout(logging.StreamHandler[TextIO]):
    """Writes to whatever standard output is *now*, not what it was when configured.

    A test runner swaps standard output for every test; holding on to the first one
    would write into a closed file.
    """

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self) -> TextIO:
        """The current standard output."""
        return sys.stdout

    @stream.setter
    def stream(self, value: TextIO) -> None:
        """Ignore being told a stream: it is looked up on every write."""


def redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Blank the value of every key that looks like it holds a secret.

    Args:
        _logger: Unused.
        _method: Unused.
        event_dict: The event being logged.

    Returns:
        The same event, with secret-looking values replaced.
    """
    for name in event_dict:
        lowered = name.lower()
        if any(word in lowered for word in SECRET_WORDS):
            event_dict[name] = REDACTED
    return event_dict


def _shared() -> list[Processor]:
    """The steps every line goes through, whoever wrote it."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.ExtraAdder(),
        redact,
        structlog.processors.StackInfoRenderer(),
    ]


def use_json(log_format: LogFormat, *, tty: bool | None = None) -> bool:
    """Decide whether to write JSON.

    Args:
        log_format: ``json`` or ``console`` to force one; ``auto`` to choose.
        tty: Whether a person is watching the output. Looked up when omitted.

    Returns:
        ``True`` for JSON.
    """
    if log_format != "auto":
        return log_format == "json"
    watching = sys.stdout.isatty() if tty is None else tty
    return not watching


def configure_logging(
    level: str = "INFO", log_format: LogFormat = "auto", *, stream: TextIO | None = None
) -> None:
    """Set up structlog and route the standard library's logging through it.

    Safe to call again: it replaces what an earlier call installed and leaves any other
    handler (a test's capture, say) alone.

    Args:
        level: The least severe level to show.
        log_format: ``json``, ``console`` or ``auto``.
        stream: Where to write. Standard output when omitted.
    """
    shared = _shared()
    as_json = use_json(log_format)
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if as_json
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Off, so a test can capture logs even for a logger created earlier.
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler: logging.Handler = (
        _CurrentStdout() if stream is None else logging.StreamHandler(stream)
    )
    handler.setFormatter(formatter)
    setattr(handler, _OWNED, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _OWNED, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper() if level.upper() in LEVELS else "INFO")
    for name, quiet in _QUIET.items():
        logging.getLogger(name).setLevel(max(quiet, root.level))
