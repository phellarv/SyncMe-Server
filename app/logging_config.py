import json
import logging
import logging.config
from pathlib import Path
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


LOG_CONFIG_PATH = Path(__file__).resolve().parent.parent / "log_config.json"


def load_logging_config() -> Dict[str, Any]:
    """Load logging configuration from the central log_config.json file."""
    with open(LOG_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


LOGGING_CONFIG: Dict[str, Any] = load_logging_config()


def configure_logging() -> None:
    """Configures standard logging and Uvicorn formatters with timestamps."""
    logging.config.dictConfig(LOGGING_CONFIG)
