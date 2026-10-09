"""Tests for server fixes:
1. Broadcaster echo suppression (origin_client_id matching owner).
2. Delete on already deleted file raising HTTPException 404.
3. Snapshot consistency in get_state during concurrent writes.
"""

import asyncio
import sqlite3
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app import db
from app.events import Broadcaster
from app.main import app

AUTH_HEADER = {"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"}


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ============================================================================
# Criterion 1: Echo suppression in Broadcaster
# ============================================================================


@pytest.mark.asyncio
async def test_broadcaster_echo_suppression():
    """Criterion 1: Echo suppression in Broadcaster.

    Opprett en Broadcaster, subscribe med to klienter:
    - clientA med owner="laptop"
    - clientB med owner="phone"
    Når b.broadcast(..., origin_client_id="laptop") kalles, skal clientB motta meldingen,
    mens clientA IKKE skal motta den (køen skal være tom).
    """
    b = Broadcaster()
    q_a = b.subscribe("ws1", "clientA", owner="laptop")
    q_b = b.subscribe("ws1", "clientB", owner="phone")

    await b.broadcast("ws1", "file_updated", "doc.txt", 1, origin_client_id="laptop")

    # clientA was the origin ("laptop"), so its queue must be empty
    assert q_a.empty(), "Echo suppression failed: sender queue received its own broadcast"

    # clientB has owner="phone", so it must receive the message
    assert not q_b.empty(), "Receiver queue did not receive broadcast"
    msg = q_b.get_nowait()
    assert msg["path"] == "doc.txt"
    assert msg["origin_client_id"] == "laptop"


@pytest.mark.asyncio
async def test_broadcaster_no_echo_suppression_when_origin_is_none():
    """Criterion 1 (boundary): Broadcast without origin_client_id delivers to all subscribers."""
    b = Broadcaster()
    q_a = b.subscribe("ws1", "clientA", owner="laptop")
    q_b = b.subscribe("ws1", "clientB", owner="phone")

    await b.broadcast("ws1", "file_updated", "doc.txt", 1, origin_client_id=None)

    assert not q_a.empty()
    assert not q_b.empty()


@pytest.mark.asyncio
async def test_broadcaster_echo_suppression_multiple_sessions_same_owner():
    """Criterion 1 (boundary): All subscriber queues with matching owner are suppressed."""
    b = Broadcaster()
    q_a1 = b.subscribe("ws1", "clientA1", owner="laptop")
    q_a2 = b.subscribe("ws1", "clientA2", owner="laptop")
    q_b = b.subscribe("ws1", "clientB", owner="phone")

    await b.broadcast("ws1", "file_updated", "doc.txt", 1, origin_client_id="laptop")

    assert q_a1.empty(), "Session 1 of origin client received echo"
    assert q_a2.empty(), "Session 2 of origin client received echo"
    assert not q_b.empty(), "Other client did not receive message"


# ============================================================================
# Criterion 2: Delete on already deleted file raises 404
# ============================================================================


def test_delete_already_deleted_file_raises_404():
    """Criterion 2: Delete på allerede slettet fil.

    Når en fil er slettet (is_deleted = 1 i databasen), skal påfølgende db.delete_file()
    kaste HTTPException med status 404.
    """
    # First, create and then delete the file
    db.upsert_file("ws1", "test.txt", "abc123hash", 12, 1000.0)
    rev_after_del = db.delete_file("ws1", "test.txt")
    assert rev_after_del > 0

    # Verify that the record in DB has is_deleted = 1
    conn = db.get_conn()
    row = conn.execute("SELECT is_deleted FROM files WHERE workspace_id = 'ws1' AND path = 'test.txt'").fetchone()
    assert row is not None
    assert row["is_deleted"] == 1

    # Second delete on already deleted file must raise HTTPException(status_code=404)
    with pytest.raises(HTTPException) as exc_info:
        db.delete_file("ws1", "test.txt")

    assert exc_info.value.status_code == 404, f"Expected 404, got {exc_info.value.status_code}"


def test_delete_nonexistent_file_raises_404():
    """Criterion 2 (boundary): db.delete_file() on a never-existing file raises 404."""
    with pytest.raises(HTTPException) as exc_info:
        db.delete_file("ws1", "never_existed.txt")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_already_deleted_file_via_api_returns_404(client):
    """Criterion 2 (integration): Calling DELETE on already deleted file via HTTP API returns 404."""
    content = b"sample content"
    # Create file
    put_resp = await client.put("/api/v1/workspaces/ws1/files/to_delete.txt", content=content, headers=AUTH_HEADER)
    assert put_resp.status_code == 200

    # First delete succeeds
    del1_resp = await client.delete("/api/v1/workspaces/ws1/files/to_delete.txt", headers=AUTH_HEADER)
    assert del1_resp.status_code == 200

    # Second delete must return 404 Not Found
    del2_resp = await client.delete("/api/v1/workspaces/ws1/files/to_delete.txt", headers=AUTH_HEADER)
    assert del2_resp.status_code == 404, f"Expected 404 on re-deleting, got {del2_resp.status_code}"


# ============================================================================
# Criterion 3: Snapshot consistency in get_state
# ============================================================================


def test_get_state_snapshot_consistency():
    """Criterion 3: Snapshot consistency i get_state.

    Verifiser at get_state(workspace_id) returnerer konsistent latest_revision
    innenfor transaksjonen. Hvis en samtidig skriving oppdaterer revisjonen etter
    at files-tabellen er lest, må get_state likevel returnere en snapshot-konsistent
    latest_revision (som svarer til tilstanden til files som ble lest).
    """
    # Seed DB with revision 1
    db.upsert_file("ws1", "file1.txt", "hash1", 100, 1000.0)

    # Create a proxy wrapper around conn to intercept execute
    class ConnProxy:
        def __init__(self, target):
            self._target = target

        def execute(self, sql, *args, **kwargs):
            cursor = self._target.execute(sql, *args, **kwargs)
            if isinstance(sql, str) and "SELECT * FROM files" in sql:
                conn2 = sqlite3.connect(db.get_db_path(), isolation_level=None)
                conn2.execute("PRAGMA journal_mode=WAL;")
                conn2.execute(
                    "INSERT INTO files (workspace_id, path, sha256, size, mtime, revision, is_deleted) "
                    "VALUES ('ws1', 'file2.txt', 'hash2', 200, 2000.0, 2, 0)"
                )
                conn2.execute("UPDATE metadata SET value = '2' WHERE key = 'current_revision:ws1'")
                conn2.close()
            return cursor

        def __enter__(self):
            return self._target.__enter__()

        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._target.__exit__(exc_type, exc_val, exc_tb)

        def __getattr__(self, name):
            return getattr(self._target, name)

    real_conn = db.get_conn()
    proxy = ConnProxy(real_conn)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(db, "get_conn", lambda: proxy)

    try:
        files, latest_revision = db.get_state("ws1")
        file_revisions = [f["revision"] for f in files]
        max_file_rev = max(file_revisions) if file_revisions else 0
        assert latest_revision == max_file_rev, (
            f"Snapshot inconsistency detected: get_state returned files with max revision {max_file_rev}, "
            f"but latest_revision={latest_revision}"
        )
    finally:
        monkeypatch.undo()


# ============================================================================
# Criterion 4: SSE keepalive
# ============================================================================


@pytest.mark.asyncio
async def test_sse_events_keepalive(monkeypatch):
    """Criterion 4: sse_events sends keepalive comments when no message is received."""
    from unittest.mock import AsyncMock, MagicMock
    from fastapi.security import HTTPAuthorizationCredentials
    from app import main
    from app.keys import keystore

    keystore.set_keys({"testclient": "dummy-key"})
    monkeypatch.setattr(main, "SSE_REVALIDATE_INTERVAL", 0.01)

    req = MagicMock()
    req.is_disconnected = AsyncMock(return_value=False)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="dummy-key")

    resp = await main.sse_events("ws1", req, "testclient", creds)
    gen = resp.body_iterator
    first = await anext(gen)
    assert first == {"comment": "keepalive"}


