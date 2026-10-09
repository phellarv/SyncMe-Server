import time
import pytest
import yaml
from pathlib import Path

from app import db, events
from app.config import PROJECT_ROOT, settings
from app.keys import _write
from app.cli import main

KEY1 = "test-key-laptop-0123456789abcdef01234567"
KEY2 = "test-key-workstation-0123456789abcdef012"
KEY3 = "test-key-offline-0123456789abcdef0123456"
KEY4 = "test-key-never-0123456789abcdef012345678"


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    keys_file = tmp_path / "keys" / "keys.toml"
    keys_file.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings, "DB_PATH", data_dir / "server_state.db")
    monkeypatch.setattr(settings, "SYNCME_KEYS_FILE", keys_file)
    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path / "storage")

    if hasattr(db._local, "conn"):
        del db._local.conn

    # Write keys.toml
    clients = {
        "laptop": {"key": KEY1, "created": "2026-10-01"},
        "workstation": {"key": KEY2, "created": "2026-10-02"},
        "offline-box": {"key": KEY3, "created": "2026-10-03"},
        "never-seen": {"key": KEY4, "created": "2026-10-04"},
    }
    _write(keys_file, clients)

    # Clear event broadcaster
    events.broadcaster.workspaces.clear()

    yield

    if hasattr(db._local, "conn"):
        del db._local.conn
    events.broadcaster.workspaces.clear()


def test_cli_clients_command(capsys):
    now = time.time()
    # laptop: online (<120s)
    db.record_client_activity("laptop", "ws1", revision=5, ip="192.168.1.50")
    db.record_client_activity("laptop", "ws2", revision=2, ip="192.168.1.50")
    # Manually backdate workstation: 15 minutes ago
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO client_state (client_name, workspace_id, last_seen, last_synced_revision, last_ip) VALUES (?, ?, ?, ?, ?)",
        ("workstation", "ws1", now - 900, 3, "192.168.1.60")
    )
    # offline-box: 2 days ago
    conn.execute(
        "INSERT INTO client_state (client_name, workspace_id, last_seen, last_synced_revision, last_ip) VALUES (?, ?, ?, ?, ?)",
        ("offline-box", "ws3", now - 172800, 1, "10.0.0.1")
    )

    ret = main(["clients"])
    assert ret == 0

    out = capsys.readouterr().out
    assert "Client" in out
    assert "Status" in out
    assert "Last Seen" in out
    assert "Last IP" in out
    assert "Active Workspaces" in out

    assert "laptop" in out
    assert "Online (SSE)" in out
    assert "ws1, ws2" in out or "ws1" in out

    assert "workstation" in out
    assert "Idle (15m ago)" in out

    assert "offline-box" in out
    assert "Offline (2d ago)" in out

    assert "never-seen" in out
    assert "Never connected" in out


def test_cli_clients_active_sse(capsys):
    now = time.time()
    # Seen 10 minutes ago, but active SSE connection exists
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO client_state (client_name, workspace_id, last_seen, last_synced_revision, last_ip) VALUES (?, ?, ?, ?, ?)",
        ("workstation", "ws1", now - 600, 3, "192.168.1.60")
    )
    events.broadcaster.subscribe("ws1", "client-sub-1", owner="workstation")

    ret = main(["clients"])
    assert ret == 0

    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "workstation" in l]
    assert len(lines) == 1
    assert "Online (SSE)" in lines[0]


def test_cli_status_all_synced(capsys):
    db.upsert_file("ws1", "hello.txt", "h1", 1024, time.time(), client_name="laptop")
    db.record_client_activity("laptop", "ws1", revision=1, ip="192.168.1.50")
    db.record_client_activity("workstation", "ws1", revision=1, ip="192.168.1.60")

    ret = main(["status"])
    assert ret == 0

    out = capsys.readouterr().out
    assert "Workspace" in out
    assert "Rev" in out
    assert "Files" in out
    assert "Size" in out
    assert "Last Modified" in out
    assert "By Client" in out
    assert "Client Lag" in out

    assert "ws1" in out
    assert "1.0 KB" in out or "1024 B" in out
    assert "laptop" in out
    assert "all synced" in out


def test_cli_status_lagging_clients(capsys):
    # Server rev will be 3
    db.upsert_file("ws1", "f1.txt", "h1", 500, time.time(), client_name="laptop")
    db.upsert_file("ws1", "f2.txt", "h2", 500, time.time(), client_name="laptop")
    db.upsert_file("ws1", "f3.txt", "h3", 500, time.time(), client_name="laptop")

    db.record_client_activity("laptop", "ws1", revision=3, ip="192.168.1.50")
    db.record_client_activity("workstation", "ws1", revision=1, ip="192.168.1.60")

    ret = main(["status"])
    assert ret == 0

    out = capsys.readouterr().out
    assert "ws1" in out
    assert "laptop (synced)" in out
    assert "workstation (-2)" in out


def test_cli_client_status(capsys):
    db.upsert_file("ws1", "notes.txt", "h1", 1200, time.time(), client_name="laptop")
    db.delete_file("ws1", "notes.txt", client_name="laptop")
    db.record_client_activity("laptop", "ws1", revision=2, ip="192.168.1.50")

    ret = main(["client", "status", "laptop"])
    assert ret == 0

    out = capsys.readouterr().out
    assert "laptop" in out
    assert "2026-10-01" in out
    assert "192.168.1.50" in out
    assert "Online (SSE)" in out

    # Workspace Sync State table
    assert "Workspace" in out
    assert "Client Rev" in out
    assert "Server Rev" in out
    assert "Lag" in out
    assert "Last Synced" in out
    assert "ws1" in out

    # Recent File Activity table
    assert "File Path" in out
    assert "Action" in out
    assert "notes.txt" in out
    assert "Deleted" in out
    assert "Updated" in out


def test_cli_client_status_unknown_client(capsys):
    ret = main(["client", "status", "nonexistent"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "nonexistent" in err
    assert "not found" in err


def test_docker_compose_cli_service():
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "cli" in compose["services"]
    cli_svc = compose["services"]["cli"]
    assert cli_svc["profiles"] == ["tools"]
    assert cli_svc["entrypoint"] == ["python", "-m", "app.cli"]
    assert "${KEYS_DIR:-./keys}:/keys:ro" in cli_svc["volumes"]
    assert "${DATA_DIR:-./data}:/data" in cli_svc["volumes"]
    assert "${STORAGE_DIR:-./storage}:/storage:ro" in cli_svc["volumes"]
