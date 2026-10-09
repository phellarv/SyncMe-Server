import logging
import logging.config
import re
from typing import Any, Dict
from urllib.parse import unquote_plus

DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Query parameters that may carry a credential. Only the Authorization header is accepted by the API, but a
# client that still sends ?token=<key> must not get its key written into the access log.
SENSITIVE_QUERY_PARAMS = frozenset({"token", "access_token", "api_key", "key"})
_QUERY_PARAM_RE = re.compile(r'(?P<sep>[?&;])(?P<name>[^=&;?#\s"]+)=(?P<value>[^&;#\s"]*)')


def redact_query(text: str) -> str:
    """Replace the value of every sensitive query parameter in a request line or URL with ***."""

    def _redact(m: re.Match) -> str:
        if unquote_plus(m["name"]).lower() in SENSITIVE_QUERY_PARAMS:
            return f"{m['sep']}{m['name']}=***"
        return m.group(0)

    return _QUERY_PARAM_RE.sub(_redact, text)


class RedactQueryTokensFilter(logging.Filter):
    """Redact credentials in the request line of uvicorn's access log records (never drops a record)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.args:
            if isinstance(record.msg, str):          # a format string that has arguments is left alone
                record.msg = redact_query(record.msg)
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_query(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_query(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True

LOGGING_CONFIG: Dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(asctime)s - %(levelprefix)s %(message)s",
            "datefmt": DEFAULT_DATE_FORMAT,
            "use_colors": None,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(asctime)s - %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "datefmt": DEFAULT_DATE_FORMAT,
        },
    },
    "filters": {
        "redact_query_tokens": {"()": "app.logging_config.RedactQueryTokensFilter"},
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "filters": ["redact_query_tokens"],
        },
    },
    "loggers": {
        "": {"handlers": ["default"], "level": "INFO"},
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


def configure_logging() -> None:
    """Configures standard logging and Uvicorn formatters with timestamps."""
    logging.config.dictConfig(LOGGING_CONFIG)
