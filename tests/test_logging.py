"""Tests for server log timestamp configuration (TDD Gate 2)."""

import io
import json
import logging
from pathlib import Path
import re
import pytest


def test_configure_logging_formatters():
    """Criterion 1: configure_logging() updates root, uvicorn, error, and access loggers with timestamp formatters."""
    from app.logging_config import configure_logging, LOGGING_CONFIG, DEFAULT_DATE_FORMAT

    # Verify exposed constants
    assert DEFAULT_DATE_FORMAT == "%Y-%m-%d %H:%M:%S"
    assert isinstance(LOGGING_CONFIG, dict)

    # Apply logging configuration
    configure_logging()

    # Verify handlers are updated for root and uvicorn loggers
    loggers_to_check = ["", "uvicorn", "uvicorn.error", "uvicorn.access"]
    for name in loggers_to_check:
        log = logging.getLogger(name)
        assert len(log.handlers) > 0, f"Logger '{name or 'root'}' has no handlers configured"
        assert log.handlers[0].formatter is not None, f"Logger '{name or 'root'}' handler has no formatter"

    # Default formatter test: timestamp matches YYYY-MM-DD HH:MM:SS at start of log
    default_formatter = logging.getLogger("uvicorn.error").handlers[0].formatter
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Test default message",
        args=(),
        exc_info=None,
    )
    formatted_default = default_formatter.format(record)
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", formatted_default), (
        f"Default log does not start with timestamp YYYY-MM-DD HH:MM:SS: {formatted_default!r}"
    )

    # Access formatter test: timestamp matches YYYY-MM-DD HH:MM:SS at start of log
    access_formatter = logging.getLogger("uvicorn.access").handlers[0].formatter
    access_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:12345", "GET", "/health", "1.1", 200),
        exc_info=None,
    )
    formatted_access = access_formatter.format(access_record)
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", formatted_access), (
        f"Access log does not start with timestamp YYYY-MM-DD HH:MM:SS: {formatted_access!r}"
    )


def test_app_log_output_contains_timestamp():
    """Criterion 2: Application log output contains timestamp format YYYY-MM-DD HH:MM:SS and message."""
    from app.logging_config import configure_logging

    configure_logging()
    logger = logging.getLogger("uvicorn.error")

    # Capture log stream using the active handler's formatter
    stream = io.StringIO()
    test_handler = logging.StreamHandler(stream)
    formatter = logger.handlers[0].formatter if logger.handlers else logging.getLogger().handlers[0].formatter
    test_handler.setFormatter(formatter)

    logger.addHandler(test_handler)
    try:
        test_msg = "Test log message"
        logger.info(test_msg)
        output = stream.getvalue()

        # Output must contain YYYY-MM-DD HH:MM:SS and the log message
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", output) is not None, (
            f"Log output did not contain YYYY-MM-DD HH:MM:SS timestamp: {output!r}"
        )
        assert test_msg in output, f"Log output did not contain original message: {output!r}"
    finally:
        logger.removeHandler(test_handler)


def test_log_config_json_validity():
    """Criterion 3: log_config.json exists, adheres to dictConfig schema, and specifies timestamp format."""
    from app.logging_config import LOGGING_CONFIG

    config_path = Path(__file__).resolve().parent.parent / "log_config.json"
    assert config_path.is_file(), f"Expected log_config.json to exist at {config_path}"

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data == LOGGING_CONFIG, "log_config.json diverged from app.logging_config.LOGGING_CONFIG"

    # Valid Uvicorn / python dictConfig structure
    assert data.get("version") == 1, "Config must have version 1"
    assert "formatters" in data, "Config missing 'formatters' section"
    assert "handlers" in data, "Config missing 'handlers' section"
    assert "loggers" in data, "Config missing 'loggers' section"

    formatters = data["formatters"]
    assert "default" in formatters, "Formatters missing 'default'"
    assert "access" in formatters, "Formatters missing 'access'"

    # Timestamp assertions
    assert formatters["default"].get("datefmt") == "%Y-%m-%d %H:%M:%S", (
        f"Default datefmt must be '%Y-%m-%d %H:%M:%S', got {formatters['default'].get('datefmt')!r}"
    )
    assert "%(asctime)s" in formatters["default"].get("fmt", ""), (
        "Default fmt must include '%(asctime)s'"
    )

    assert formatters["access"].get("datefmt") == "%Y-%m-%d %H:%M:%S", (
        f"Access datefmt must be '%Y-%m-%d %H:%M:%S', got {formatters['access'].get('datefmt')!r}"
    )
    assert "%(asctime)s" in formatters["access"].get("fmt", ""), (
        "Access fmt must include '%(asctime)s'"
    )


# --- access log: credentials in the query string must never reach the log -------------------------

SECRET = "s3cretvalue-not-a-real-key"
ACCESS_FMT = '%s - "%s %s HTTP/%s" %d'     # the arguments uvicorn passes for every request


@pytest.mark.parametrize(
    "line, expected",
    [
        (f"/x?token={SECRET}", "/x?token=***"),
        (f"/x?a=1&token={SECRET}&b=2", "/x?a=1&token=***&b=2"),
        (f"/x?access_token={SECRET}", "/x?access_token=***"),
        (f"/x?api_key={SECRET}", "/x?api_key=***"),
        (f"/x?key={SECRET}", "/x?key=***"),
        (f"/x?TOKEN={SECRET}", "/x?TOKEN=***"),
        (f"/x?%74oken={SECRET}", "/x?%74oken=***"),                 # percent-encoded parameter name
        (f"/x?a=1;token={SECRET}", "/x?a=1;token=***"),
        (f"/x?token={SECRET}&key={SECRET}", "/x?token=***&key=***"),
        (f"/x?token={SECRET}#frag", "/x?token=***#frag"),
        ("/x?monkey=1&token_id=5&keyboard=2&mytoken=3", "/x?monkey=1&token_id=5&keyboard=2&mytoken=3"),
        ("/token=abc/key=def", "/token=abc/key=def"),               # not a query string
        ("/plain", "/plain"),
    ],
)
def test_redact_query_replaces_sensitive_values_only(line, expected):
    from app.logging_config import redact_query

    assert redact_query(line) == expected


def test_filter_redacts_string_arguments_and_keeps_the_others():
    from app.logging_config import RedactQueryTokensFilter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, ACCESS_FMT,
        ("127.0.0.1:1", "GET", f"/x?token={SECRET}", "1.1", 200), None,
    )
    assert RedactQueryTokensFilter().filter(record) is True        # the record is still logged
    assert record.args == ("127.0.0.1:1", "GET", "/x?token=***", "1.1", 200)
    assert SECRET not in record.getMessage()


def test_filter_redacts_mapping_arguments():
    from app.logging_config import RedactQueryTokensFilter

    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "%(path)s -> %(status)d", ({"path": f"/x?token={SECRET}", "status": 200},), None
    )
    RedactQueryTokensFilter().filter(record)
    assert record.getMessage() == "/x?token=*** -> 200"


def test_filter_redacts_a_preformatted_message_but_never_touches_a_format_string_with_arguments():
    from app.logging_config import RedactQueryTokensFilter

    plain = logging.LogRecord("x", logging.INFO, __file__, 1, f"GET /x?token={SECRET}", (), None)
    RedactQueryTokensFilter().filter(plain)
    assert plain.getMessage() == "GET /x?token=***"
    fmt = logging.LogRecord("x", logging.INFO, __file__, 1, "GET /x?token=%s", ("v",), None)
    RedactQueryTokensFilter().filter(fmt)
    assert fmt.getMessage() == "GET /x?token=v"                     # no "not all arguments converted"


def _access_output_through_the_configured_handler() -> str:
    access = logging.getLogger("uvicorn.access")
    stream = io.StringIO()
    access.handlers[0].setStream(stream)
    access.info(ACCESS_FMT, "127.0.0.1:1", "GET", f"/api/v1/workspaces/ws1/events?token={SECRET}&x=1", "1.1", 200)
    return stream.getvalue()


def _assert_redacted_but_logged(out: str) -> None:
    assert SECRET not in out
    assert "token=***" in out and "x=1" in out and "200" in out     # the line is still emitted


def test_access_log_is_redacted_with_logging_config():
    from app.logging_config import configure_logging

    configure_logging()
    try:
        _assert_redacted_but_logged(_access_output_through_the_configured_handler())
    finally:
        configure_logging()


def test_access_log_is_redacted_when_uvicorn_loads_log_config_json():
    """The Dockerfile starts uvicorn with --log-config log_config.json: use the same code path."""
    import uvicorn

    from app.logging_config import configure_logging

    config_path = Path(__file__).resolve().parent.parent / "log_config.json"
    uvicorn.Config("app.main:app", log_config=str(config_path))
    try:
        _assert_redacted_but_logged(_access_output_through_the_configured_handler())
    finally:
        configure_logging()
