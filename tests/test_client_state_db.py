import time
import pytest
from pathlib import Path
from httpx import AsyncClient, ASGITransport

from app import db, storage, auth
from app.config import settings
from app.keys import keystore
from app.main import app

KEY1 = "test-key-client1-0123456789abcdef01234567"
KEY2 = "test-key-client2-0123456789abcdef01234567"
H1 = {"Authorization": f"Bearer {KEY1}"}
H2 = {"Authorization": f"Bearer {KEY2}"}


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings, "DB_PATH", data_dir / "server_state.db")
    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)

    if hasattr(db._local, "conn"):
        del db._local.conn

    keystore.set_keys({"client1": KEY1, "client2": KEY2})
    yield
    if hasattr(db._local, "conn"):
        del db._local.conn


def test_client_state_table_created():
    conn = db.get_conn()
    cursor = conn.execute("PRAGMA table_info(client_state)")
    columns = {row["name"]: row["type"] for row in cursor.fetchall()}
    assert "client_name" in columns
    assert "workspace_id" in columns
    assert "last_seen" in columns
    assert "last_synced_revision" in columns
    assert "last_ip" in columns


def test_record_client_activity_basic():
    t0 = time.time()
    db.record_client_activity("client1", "ws1", revision=0, ip="192.168.1.10")

    rows = db.get_client_state("client1")
    assert len(rows) == 1
    row = rows[0]
    assert row["client_name"] == "client1"
    assert row["workspace_id"] == "ws1"
    assert row["last_synced_revision"] == 0
    assert row["last_ip"] == "192.168.1.10"
    assert row["last_seen"] >= t0


def test_record_client_activity_upsert_revision_and_ip():
    db.record_client_activity("client1", "ws1", revision=2, ip="192.168.1.10")
    row = db.get_client_state("client1")[0]
    assert row["last_synced_revision"] == 2
    assert row["last_ip"] == "192.168.1.10"

    # Higher revision bumps last_synced_revision
    db.record_client_activity("client1", "ws1", revision=5, ip="192.168.1.20")
    row = db.get_client_state("client1")[0]
    assert row["last_synced_revision"] == 5
    assert row["last_ip"] == "192.168.1.20"

    # Lower revision does NOT downgrade last_synced_revision
    db.record_client_activity("client1", "ws1", revision=3, ip="192.168.1.30")
    row = db.get_client_state("client1")[0]
    assert row["last_synced_revision"] == 5
    assert row["last_ip"] == "192.168.1.30"

    # Empty IP does not overwrite existing last_ip
    db.record_client_activity("client1", "ws1", revision=6, ip="")
    row = db.get_client_state("client1")[0]
    assert row["last_synced_revision"] == 6
    assert row["last_ip"] == "192.168.1.30"


def test_get_clients_state_multiple():
    db.record_client_activity("client1", "ws1", revision=1, ip="10.0.0.1")
    db.record_client_activity("client1", "ws2", revision=3, ip="10.0.0.1")
    db.record_client_activity("client2", "ws1", revision=2, ip="10.0.0.2")

    all_state = db.get_clients_state()
    assert len(all_state) == 3

    client1_state = db.get_client_state("client1")
    assert len(client1_state) == 2
    workspaces = {r["workspace_id"] for r in client1_state}
    assert workspaces == {"ws1", "ws2"}

    client2_state = db.get_client_state("client2")
    assert len(client2_state) == 1
    assert client2_state[0]["workspace_id"] == "ws1"


def test_get_workspaces_summary():
    # Set up files in ws1 and ws2
    db.upsert_file("ws1", "file1.txt", "hash1", 100, 1000.0, client_name="client1")
    db.upsert_file("ws1", "file2.txt", "hash2", 200, 2000.0, client_name="client2")
    db.upsert_file("ws2", "doc.txt", "hash3", 500, 3000.0, client_name="client1")

    summaries = db.get_workspaces_summary()
    assert len(summaries) == 2
    ws_map = {s["workspace_id"]: s for s in summaries}

    ws1 = ws_map["ws1"]
    assert ws1["workspace_id"] == "ws1"
    assert ws1["current_revision"] == 2
    assert ws1["file_count"] == 2
    assert ws1["total_size"] == 300
    assert ws1["last_client"] == "client2"

    ws2 = ws_map["ws2"]
    assert ws2["workspace_id"] == "ws2"
    assert ws2["current_revision"] == 1
    assert ws2["file_count"] == 1
    assert ws2["total_size"] == 500
    assert ws2["last_client"] == "client1"

    # Delete file2 in ws1
    db.delete_file("ws1", "file2.txt", client_name="client1")
    summaries = db.get_workspaces_summary()
    ws1 = {s["workspace_id"]: s for s in summaries}["ws1"]
    assert ws1["current_revision"] == 3
    assert ws1["file_count"] == 1
    assert ws1["total_size"] == 100
    assert ws1["last_client"] == "client1"


def test_get_client_recent_files():
    t1 = 100.0
    t2 = 200.0
    t3 = 300.0
    db.upsert_file("ws1", "f1.txt", "h1", 10, t1, client_name="client1")
    db.upsert_file("ws1", "f2.txt", "h2", 20, t2, client_name="client2")
    db.upsert_file("ws1", "f3.txt", "h3", 30, t3, client_name="client1")
    db.delete_file("ws1", "f1.txt", client_name="client1")

    recent = db.get_client_recent_files("client1", limit=10)
    assert len(recent) == 3
    assert recent[0]["path"] == "f1.txt"
    assert recent[0]["is_deleted"] == 1
    assert recent[1]["path"] == "f3.txt"
    assert recent[1]["is_deleted"] == 0
    assert recent[2]["path"] == "f1.txt"
    assert recent[2]["is_deleted"] == 0

    # Test limit
    recent_limited = db.get_client_recent_files("client1", limit=2)
    assert len(recent_limited) == 2


@pytest.mark.asyncio
async def test_server_activity_recording_in_main():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. upload_file (PUT)
        r = await client.put(
            "/api/v1/workspaces/ws1/files/note.txt",
            content=b"hello world",
            headers={**H1, "X-Forwarded-For": "1.2.3.4"},
        )
        assert r.status_code == 200
        state = db.get_client_state("client1")
        assert len(state) == 1
        assert state[0]["last_synced_revision"] == 1
        assert state[0]["last_ip"] == "1.2.3.4"

        # 2. get_files / sync/state (GET)
        r = await client.get(
            "/api/v1/workspaces/ws1/files?since_revision=5",
            headers={**H2, "X-Forwarded-For": "5.6.7.8"},
        )
        assert r.status_code == 200
        state = db.get_client_state("client2")
        assert len(state) == 1
        assert state[0]["last_synced_revision"] == 5
        assert state[0]["last_ip"] == "5.6.7.8"

        # 3. delete_file (DELETE)
        r = await client.delete(
            "/api/v1/workspaces/ws1/files/note.txt",
            headers={**H1, "X-Forwarded-For": "1.2.3.9"},
        )
        assert r.status_code == 200
        state = db.get_client_state("client1")
        assert state[0]["last_synced_revision"] == 2
        assert state[0]["last_ip"] == "1.2.3.9"


def test_sse_connections_tracking():
    assert db.is_client_sse_active("client1") is False
    db.register_sse_connection("conn-1", "client1", "ws1")
    assert db.is_client_sse_active("client1") is True
    assert db.is_client_sse_active("client2") is False

    db.touch_sse_connection("conn-1")
    assert db.is_client_sse_active("client1") is True

    db.unregister_sse_connection("conn-1")
    assert db.is_client_sse_active("client1") is False


def test_prune_stale_sse_connections():
    conn = db.get_conn()
    old_time = time.time() - 120
    fresh_time = time.time()
    with conn:
        conn.execute(
            "INSERT INTO sse_connections (conn_id, client_name, workspace_id, last_seen) VALUES (?, ?, ?, ?)",
            ("stale-conn", "client1", "ws1", old_time),
        )
        conn.execute(
            "INSERT INTO sse_connections (conn_id, client_name, workspace_id, last_seen) VALUES (?, ?, ?, ?)",
            ("fresh-conn", "client2", "ws1", fresh_time),
        )
    assert db.is_client_sse_active("client1", ttl=60) is False
    assert db.is_client_sse_active("client2", ttl=60) is True
    assert db.prune_stale_sse_connections(max_age=60) == 1
    assert db.is_client_sse_active("client2", ttl=60) is True
