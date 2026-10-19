import hashlib
import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.db as db
from app.config import settings
from app.events import Broadcaster
from app.main import app
from app.validation import normalize_path

AUTH_HEADERS = {"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"}


@pytest.fixture(autouse=True)
def setup_teardown():
    settings.DATA_DIR = Path("./test_data")
    if settings.DATA_DIR.exists():
        shutil.rmtree(settings.DATA_DIR)
    settings.DATA_DIR.mkdir(parents=True)

    if hasattr(db._local, "conn"):
        del db._local.conn

    yield

    if settings.DATA_DIR.exists():
        shutil.rmtree(settings.DATA_DIR)


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_recreate_deleted_file_with_if_match_zero(client):
    r1 = await client.put(
        "/api/v1/workspaces/ws1/files/doc.txt",
        content=b"v1",
        headers={**AUTH_HEADERS, "If-Match": "0"},
    )
    assert r1.status_code == 200
    rev1 = r1.json()["revision"]

    r_del = await client.delete(
        "/api/v1/workspaces/ws1/files/doc.txt",
        headers={**AUTH_HEADERS, "If-Match": str(rev1)},
    )
    assert r_del.status_code == 200
    rev_del = r_del.json()["revision"]

    r2 = await client.put(
        "/api/v1/workspaces/ws1/files/doc.txt",
        content=b"v2",
        headers={**AUTH_HEADERS, "If-Match": "0"},
    )
    assert r2.status_code == 200
    rev2 = r2.json()["revision"]
    assert rev2 > rev_del

    r_get = await client.get("/api/v1/workspaces/ws1/files/doc.txt", headers=AUTH_HEADERS)
    assert r_get.status_code == 200
    assert r_get.content == b"v2"


@pytest.mark.asyncio
async def test_update_deleted_file_with_stale_if_match_returns_409_with_conflict_headers(client):
    r1 = await client.put(
        "/api/v1/workspaces/ws1/files/doc.txt",
        content=b"v1",
        headers={**AUTH_HEADERS, "If-Match": "0"},
    )
    rev1 = r1.json()["revision"]

    r_del = await client.delete(
        "/api/v1/workspaces/ws1/files/doc.txt",
        headers={**AUTH_HEADERS, "If-Match": str(rev1)},
    )
    rev_del = r_del.json()["revision"]

    r_stale = await client.put(
        "/api/v1/workspaces/ws1/files/doc.txt",
        content=b"v2",
        headers={**AUTH_HEADERS, "If-Match": str(rev1)},
    )
    assert r_stale.status_code == 409
    assert r_stale.headers.get("x-syncme-conflict-reason") == "deleted"
    assert r_stale.headers.get("x-syncme-current-revision") == "0"
    assert r_stale.headers.get("x-syncme-head-revision") == str(rev_del)
    assert r_stale.headers.get("x-syncme-current-sha256") == ""


@pytest.mark.asyncio
async def test_update_active_file_with_stale_if_match_returns_409_with_conflict_headers(client):
    r1 = await client.put(
        "/api/v1/workspaces/ws1/files/doc.txt",
        content=b"v1",
        headers={**AUTH_HEADERS, "If-Match": "0"},
    )
    rev1 = r1.json()["revision"]

    r2 = await client.put(
        "/api/v1/workspaces/ws1/files/doc.txt",
        content=b"v2",
        headers={**AUTH_HEADERS, "If-Match": str(rev1)},
    )
    rev2 = r2.json()["revision"]

    r_stale = await client.put(
        "/api/v1/workspaces/ws1/files/doc.txt",
        content=b"v3",
        headers={**AUTH_HEADERS, "If-Match": str(rev1)},
    )
    assert r_stale.status_code == 409
    assert r_stale.headers.get("x-syncme-conflict-reason") == "revision_mismatch"
    assert r_stale.headers.get("x-syncme-current-revision") == str(rev2)
    assert r_stale.headers.get("x-syncme-head-revision") == str(rev2)
    assert r_stale.headers.get("x-syncme-current-sha256") == hashlib.sha256(b"v2").hexdigest()


@pytest.mark.asyncio
async def test_idempotent_put_retry_with_identical_sha256_succeeds(client):
    r1 = await client.put(
        "/api/v1/workspaces/ws1/files/doc.txt",
        content=b"same-payload",
        headers={**AUTH_HEADERS, "If-Match": "0"},
    )
    assert r1.status_code == 200
    rev1 = r1.json()["revision"]

    # Retry the exact same request with stale If-Match: 0 and identical payload
    r2 = await client.put(
        "/api/v1/workspaces/ws1/files/doc.txt",
        content=b"same-payload",
        headers={**AUTH_HEADERS, "If-Match": "0"},
    )
    assert r2.status_code == 200
    assert r2.json()["revision"] == rev1


@pytest.mark.asyncio
async def test_normalize_path_unicode_nfc_and_reject_conflict_artifacts(client):
    assert normalize_path("cafe\u0301.txt") == "caf\u00e9.txt"

    r_put = await client.put(
        "/api/v1/workspaces/ws1/files/cafe%CC%81.txt",
        content=b"coffee",
        headers=AUTH_HEADERS,
    )
    assert r_put.status_code == 200

    r_state = await client.get("/api/v1/workspaces/ws1/sync/state", headers=AUTH_HEADERS)
    assert r_state.status_code == 200
    paths = [f["path"] for f in r_state.json()["files"]]
    assert paths == ["caf\u00e9.txt"]

    # Conflict artifacts must be rejected on normal path validation
    r_conflict = await client.put(
        "/api/v1/workspaces/ws1/files/doc.conflict.20260101_000000.txt",
        content=b"conflict",
        headers=AUTH_HEADERS,
    )
    assert r_conflict.status_code == 400

    # Ordinary filenames containing the word 'conflict' without numeric timestamp artifact pattern are permitted
    assert normalize_path("resolve.conflict.notes.md") == "resolve.conflict.notes.md"

    # Archive slack (>0) still allows inspecting/restoring archived files if needed
    assert normalize_path("doc.conflict.20260101_000000.txt_1700000000", slack=25) == (
        "doc.conflict.20260101_000000.txt_1700000000"
    )


@pytest.mark.asyncio
async def test_broadcaster_per_instance_echo_suppression():
    b = Broadcaster()
    q_a = b.subscribe("ws1", "conn-a", owner="testclient", instance_id="inst-A")
    q_b = b.subscribe("ws1", "conn-b", owner="testclient", instance_id="inst-B")

    await b.broadcast(
        "ws1",
        "file_updated",
        "doc.txt",
        1,
        origin_client_id="testclient",
        origin_instance_id="inst-A",
    )

    assert q_a.empty()
    assert not q_b.empty()
    msg = q_b.get_nowait()
    assert msg["path"] == "doc.txt"
    assert msg["revision"] == 1
