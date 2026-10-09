import os
import shutil
import hashlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import db, storage
from app.config import settings
from app.main import app

KEY = "test-key-0123456789abcdef0123456789abcdef"
H = {"Authorization": f"Bearer {KEY}"}
WS = "ws1"
FILE_PATH = "test_history.txt"
PUT_URL = f"/api/v1/workspaces/{WS}/files/{FILE_PATH}"
HISTORY_URL = f"/api/v1/workspaces/{WS}/history/{FILE_PATH}"


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    settings.DATA_DIR = Path("./test_data")
    shutil.rmtree(settings.DATA_DIR, ignore_errors=True)
    settings.DATA_DIR.mkdir(parents=True)
    for name in ("STORAGE_DIR", "FILES_DIR", "ARCHIVE_DIR", "DB_PATH"):
        monkeypatch.setattr(settings, name, None)
    if hasattr(db._local, "conn"):
        del db._local.conn
    
    # We need to setup a key for auth to work
    os.environ["SYNCME_KEYS_FILE"] = "./test_data/keys.txt"
    Path("./test_data/keys.txt").write_text(f"test-client:{KEY}\n")
    from app import keys
    keys._keys_cache = None
    
    yield
    shutil.rmtree(settings.DATA_DIR, ignore_errors=True)


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_history_list(client):
    # Upload first version
    r1 = await client.put(PUT_URL, content=b"v1", headers=H)
    assert r1.status_code == 200

    # Upload second version
    r2 = await client.put(PUT_URL, content=b"v2", headers=H)
    assert r2.status_code == 200

    # Upload third version
    r3 = await client.put(PUT_URL, content=b"v3", headers=H)
    assert r3.status_code == 200

    # Get history
    r = await client.get(HISTORY_URL, headers=H)
    assert r.status_code == 200
    res = r.json()
    assert "history" in res
    versions = res["history"]
    assert len(versions) == 3
    # Reverse chronological
    assert versions[0]["revision"] > versions[1]["revision"]
    assert versions[1]["revision"] > versions[2]["revision"]
    assert versions[0]["size"] == 2

    # Check unauthenticated
    r = await client.get(HISTORY_URL)
    assert r.status_code == 401

    # Check limit param
    r = await client.get(f"{HISTORY_URL}?limit=2", headers=H)
    assert r.status_code == 200
    versions = r.json()["history"]
    assert len(versions) == 2


@pytest.mark.asyncio
async def test_history_get_revision(client):
    r1 = await client.put(PUT_URL, content=b"v1", headers=H)
    assert r1.status_code == 200
    r1_rev = r1.json()["revision"]

    r2 = await client.put(PUT_URL, content=b"v2", headers=H)
    assert r2.status_code == 200
    r2_rev = r2.json()["revision"]
    
    r3 = await client.delete(PUT_URL, headers=H)
    assert r3.status_code == 200
    r3_rev = r3.json()["revision"]

    # Get v1
    r = await client.get(f"/api/v1/workspaces/{WS}/revisions/{r1_rev}/{FILE_PATH}", headers=H)
    assert r.status_code == 200
    assert r.content == b"v1"
    assert "ETag" in r.headers

    # Get v2
    r = await client.get(f"/api/v1/workspaces/{WS}/revisions/{r2_rev}/{FILE_PATH}", headers=H)
    assert r.status_code == 200
    assert r.content == b"v2"
    assert "ETag" in r.headers

    # Get deleted tombstone
    r = await client.get(f"/api/v1/workspaces/{WS}/revisions/{r3_rev}/{FILE_PATH}", headers=H)
    assert r.status_code == 404

    # Nonexistent revision
    r = await client.get(f"/api/v1/workspaces/{WS}/revisions/999/{FILE_PATH}", headers=H)
    assert r.status_code == 404

    # Unauthenticated
    r = await client.get(f"/api/v1/workspaces/{WS}/revisions/{r1_rev}/{FILE_PATH}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_history_restore(client):
    r1 = await client.put(PUT_URL, content=b"v1", headers=H)
    r2 = await client.put(PUT_URL, content=b"v2", headers=H)
    r3 = await client.put(PUT_URL, content=b"v3", headers=H)

    r1_rev = r1.json()["revision"]
    current_rev = r3.json()["revision"]

    # Restore v1
    body = {"restore_revision": r1_rev, "expected_revision": current_rev}
    r = await client.post(f"{HISTORY_URL}/restore", json=body, headers=H)
    assert r.status_code == 200
    assert r.json()["path"] == FILE_PATH
    new_rev = r.json()["revision"]
    assert new_rev > current_rev

    # Verify restored content
    r = await client.get(PUT_URL, headers=H)
    assert r.status_code == 200
    assert r.content == b"v1"

    # Restore with OCC conflict
    body = {"restore_revision": r1_rev, "expected_revision": 999}
    r = await client.post(f"{HISTORY_URL}/restore", json=body, headers=H)
    assert r.status_code == 409

    # Restore nonexistent revision
    body = {"restore_revision": 9999}
    r = await client.post(f"{HISTORY_URL}/restore", json=body, headers=H)
    assert r.status_code == 404
