import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app

KEY = "test-key-0123456789abcdef0123456789abcdef"
H = {"Authorization": f"Bearer {KEY}"}
F = "/api/v1/workspaces/ws1/files/a.txt"


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    for name in ("STORAGE_DIR", "FILES_DIR", "ARCHIVE_DIR", "DB_PATH"):
        monkeypatch.setattr(settings, name, None)
    settings.DATA_DIR = Path("./test_data")
    shutil.rmtree(settings.DATA_DIR, ignore_errors=True)
    settings.DATA_DIR.mkdir(parents=True)
    import app.db as db

    if hasattr(db._local, "conn"):
        del db._local.conn
    yield
    shutil.rmtree(settings.DATA_DIR, ignore_errors=True)


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["abc", '"3', "-1", "*", "9" * 30])
async def test_bad_if_match_is_400_not_500(client, value):
    assert (await client.put(F, content=b"x", headers={**H, "If-Match": value})).status_code == 400
    assert (await client.delete(F, headers={**H, "If-Match": value})).status_code == 400


@pytest.mark.asyncio
async def test_quoted_if_match_works(client):
    r = await client.put(F, content=b"x", headers=H)
    rev = r.json()["revision"]
    r2 = await client.put(F, content=b"y", headers={**H, "If-Match": f'"{rev}"'})
    assert r2.status_code == 200
    r3 = await client.delete(F, headers={**H, "If-Match": f'W/"{r2.json()["revision"]}"'})
    assert r3.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("n", ["-1", str(2**63), str(2**62 + 1), "abc"])
async def test_since_revision_is_bounded(client, n):
    r = await client.get(f"/api/v1/workspaces/ws1/sync/state?since_revision={n}", headers=H)
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
@pytest.mark.parametrize("n", ["0", "5", str(2**62)])
async def test_since_revision_in_range_is_accepted(client, n):
    r = await client.get(f"/api/v1/workspaces/ws1/sync/state?since_revision={n}", headers=H)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_health_does_not_leak_exception_text(client, monkeypatch, caplog):
    import app.db as db

    def boom():
        raise Exception("secret-path /data/server_state.db is locked")

    monkeypatch.setattr(db, "get_conn", boom)
    r = await client.get("/health")
    assert r.status_code == 503
    assert "secret-path" not in r.text and "server_state" not in r.text
    assert "secret-path" in caplog.text            # the detail goes to the log instead


@pytest.mark.asyncio
async def test_long_name_is_400(client):
    r = await client.put("/api/v1/workspaces/ws1/files/" + "a" * 300, content=b"x", headers=H)
    assert r.status_code == 400
