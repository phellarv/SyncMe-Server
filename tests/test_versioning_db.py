import hashlib
import os
import sqlite3

import pytest
from fastapi import HTTPException

from app import db
from app import storage
from app.db import init_db, get_conn, _local


def test_file_versions_table(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "DATA_DIR", tmp_path)
    if hasattr(_local, "conn"):
        del _local.conn
    
    conn = get_conn()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_versions'")
    assert cursor.fetchone() is not None

def test_upsert_and_delete_records_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "DATA_DIR", tmp_path)
    if hasattr(_local, "conn"):
        del _local.conn
    
    ws_id = "ws_vers1"
    path = "file1.txt"
    sha1 = "sha_1"
    
    rev1 = db.upsert_file(ws_id, path, sha1, 10, 1000.0, client_name="client_a")
    
    history = db.get_file_history(ws_id, path)
    assert len(history) == 1
    assert history[0]["revision"] == rev1
    assert history[0]["sha256"] == sha1
    assert history[0]["client_name"] == "client_a"
    assert history[0]["is_deleted"] == 0
    
    sha2 = "sha_2"
    rev2 = db.upsert_file(ws_id, path, sha2, 20, 1001.0, expected_revision=rev1, client_name="client_b")
    
    history = db.get_file_history(ws_id, path)
    assert len(history) == 2
    assert history[0]["revision"] == rev2  # DESC order
    assert history[0]["sha256"] == sha2
    assert history[0]["client_name"] == "client_b"
    assert history[1]["revision"] == rev1
    
    # Mock archive file so delete doesn't fail
    from app import storage
    monkeypatch.setattr(storage, "archive_file", lambda w, p: None)
    
    rev3 = db.delete_file(ws_id, path, expected_revision=rev2, client_name="client_c")
    history = db.get_file_history(ws_id, path)
    assert len(history) == 3
    assert history[0]["revision"] == rev3
    assert history[0]["is_deleted"] == 1
    assert history[0]["client_name"] == "client_c"
    assert history[0]["sha256"] is None
    assert history[0]["size"] == 0

def test_get_file_version(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "DATA_DIR", tmp_path)
    if hasattr(_local, "conn"):
        del _local.conn
        
    ws_id = "ws_vers2"
    path = "file2.txt"
    
    rev1 = db.upsert_file(ws_id, path, "sha_1", 10, 1000.0)
    version = db.get_file_version(ws_id, path, rev1)
    assert version is not None
    assert version["revision"] == rev1
    assert version["sha256"] == "sha_1"
    
    missing = db.get_file_version(ws_id, path, 999)
    assert missing is None


def test_index_existing_files_backfills_baseline_version(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "DATA_DIR", tmp_path)
    if hasattr(_local, "conn"):
        del _local.conn
    
    ws_id = "testws"
    # setup storage
    storage_dir = tmp_path / "storage"
    workspaces_dir = storage_dir / "workspaces"
    workspaces_dir.mkdir(parents=True)
    ws_dir = workspaces_dir / ws_id
    ws_dir.mkdir()
    
    # create a file
    rel_path = "legacy.txt"
    file_path = ws_dir / rel_path
    file_path.write_text("legacy content")
    
    sha256 = hashlib.sha256(b"legacy content").hexdigest()
    size = len(b"legacy content")
    mtime = file_path.stat().st_mtime
    
    # create a second file that is completely unindexed to test the needs_update=True branch
    rel_path_new = "new_file.txt"
    file_path_new = ws_dir / rel_path_new
    file_path_new.write_text("new content")
    
    conn = get_conn()
    conn.execute(
        "INSERT INTO files (workspace_id, path, sha256, size, mtime, revision, is_deleted) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ws_id, rel_path, sha256, size, mtime, 5, 0)
    )
    conn.execute(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        (f"current_revision:{ws_id}", "5")
    )
    conn.commit()
    
    # Call index_existing_files
    db.index_existing_files(storage_dir)
    
    # Assert for legacy.txt (needs_update=False branch)
    v_cur = conn.execute("SELECT * FROM file_versions WHERE workspace_id = ? AND path = ?", (ws_id, rel_path))
    rows = v_cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["revision"] == 5
    assert rows[0]["client_name"] == "baseline"
    assert rows[0]["sha256"] == sha256
    
    # Assert cas object exists
    cas_path = storage.get_cas_object(ws_id, sha256)
    assert os.path.exists(cas_path)
    
    # Call index_existing_files again for idempotency
    db.index_existing_files(storage_dir)
    v_cur = conn.execute("SELECT * FROM file_versions WHERE workspace_id = ? AND path = ?", (ws_id, rel_path))
    rows_second = v_cur.fetchall()
    assert len(rows_second) == 1
    
    # Assert for new_file.txt (needs_update=True branch)
    v_cur_new = conn.execute("SELECT * FROM file_versions WHERE workspace_id = ? AND path = ?", (ws_id, rel_path_new))
    rows_new = v_cur_new.fetchall()
    assert len(rows_new) == 1
    assert rows_new[0]["client_name"] == "index"
    assert db.get_revision(ws_id) >= 6
