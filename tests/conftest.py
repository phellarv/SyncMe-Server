import pytest

from app import db
from app.config import settings
from app.keys import keystore

TEST_KEY = "test-key-0123456789abcdef0123456789abcdef"


def _drop_connection():
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        del db._local.conn


@pytest.fixture(autouse=True)
def _install_test_keys(monkeypatch):
    # Settings are built at import time from the environment and a local .env. A leftover
    # SYNCME_API_KEY makes KeyStore.load() refuse to start, so neutralise it for every test.
    monkeypatch.setattr(settings, "SYNCME_API_KEY", None)
    keystore.set_keys({"testclient": TEST_KEY})
    yield
    keystore.set_keys({})


@pytest.fixture(autouse=True)
def _isolated_storage(monkeypatch, tmp_path_factory):
    # The same applies to the storage settings: a developer's .env or environment could point them
    # at a LIVE storage or database. Null them and give every test its own throwaway data directory,
    # so that no test can write outside the pytest temp directory. Tests that want a specific layout
    # set these again themselves.
    for name in ("STORAGE_DIR", "FILES_DIR", "ARCHIVE_DIR", "DB_PATH"):
        monkeypatch.setattr(settings, name, None)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path_factory.mktemp("data"))
    _drop_connection()                  # the cached connection belongs to the previous test's database
    yield
    _drop_connection()


@pytest.fixture
def raw_asgi():
    """Call the app with a hand-built ASGI scope, for requests an HTTP client would refuse to send."""
    from app.main import app

    async def call(method, path, headers, chunks=(b"",), on_receive=None):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1") if isinstance(v, str) else v)
                        for k, v in headers.items()],
            "client": ("127.0.0.1", 1), "server": ("test", 80),
        }
        pending = list(chunks)

        async def receive():
            if on_receive is not None:
                on_receive()
            if not pending:
                return {"type": "http.disconnect"}
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}

        sent = []

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")

    return call
