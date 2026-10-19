import hashlib
import os
from pathlib import Path
from app import storage

def test_cas_storage_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "DATA_DIR", tmp_path)
    
    ws_id = "ws1"
    sha256 = hashlib.sha256(b"hello").hexdigest()
    
    cas_base = storage.get_cas_base_dir()
    assert cas_base.name == "objects"
    assert cas_base.parent.name == "files"
    
    cas_dir = storage.get_cas_dir(ws_id)
    assert cas_dir == cas_base / ws_id
    
    cas_obj_path = storage.get_cas_object_path(ws_id, sha256)
    assert cas_obj_path.parent.name == sha256[:2]
    assert cas_obj_path.name == sha256[2:]
    assert cas_obj_path.parent.parent.name == ws_id

def test_store_cas_object(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "DATA_DIR", tmp_path)
    ws_id = "ws1"
    content = b"hello world"
    sha256 = hashlib.sha256(content).hexdigest()
    
    src = tmp_path / "src.txt"
    src.write_bytes(content)
    
    cas_path = storage.store_cas_object(ws_id, src, sha256)
    
    assert cas_path.exists()
    assert cas_path.read_bytes() == content
    
    # Store again to test deduplication
    src.write_bytes(b"hello world")
    cas_path2 = storage.store_cas_object(ws_id, src, sha256)
    assert cas_path2 == cas_path
    
    # Test getting object
    retrieved = storage.get_cas_object(ws_id, sha256)
    assert retrieved == cas_path
    
    retrieved_missing = storage.get_cas_object(ws_id, "0" * 64)
    assert retrieved_missing is None


def test_prune_storage(tmp_path, monkeypatch):
    from app import db
    import time

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage.settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage.settings, "DB_PATH", data_dir / "server_state.db")
    monkeypatch.setattr(storage.settings, "STORAGE_DIR", tmp_path / "storage")

    if hasattr(db._local, "conn"):
        del db._local.conn

    ws_id = "ws1"
    hashes = []
    for i in range(5):
        content = f"content {i}".encode()
        h = hashlib.sha256(content).hexdigest()
        hashes.append(h)
        src = tmp_path / f"src_{i}.txt"
        src.write_bytes(content)
        storage.store_cas_object(ws_id, src, h)
        db.upsert_file(ws_id, "doc.txt", h, len(content), time.time(), client_name="test")

    # Add an orphan CAS object not referenced in DB
    orphan_content = b"orphan content"
    orphan_hash = hashlib.sha256(orphan_content).hexdigest()
    orphan_src = tmp_path / "orphan.txt"
    orphan_src.write_bytes(orphan_content)
    orphan_cas = storage.store_cas_object(ws_id, orphan_src, orphan_hash)
    assert orphan_cas.exists()

    # Create old and new archive files
    arch_dir = storage.get_archive_dir(ws_id)
    old_arch = arch_dir / "old.txt_1000"
    new_arch = arch_dir / "new.txt_2000"
    old_arch.write_bytes(b"old archive")
    new_arch.write_bytes(b"new archive")
    old_mtime = time.time() - 10 * 86400
    os.utime(old_arch, (old_mtime, old_mtime))

    stats = storage.prune_storage(keep_versions=2, archive_days=5)
    assert stats["versions_deleted"] == 3
    # 3 pruned versions + 1 orphan = 4 CAS objects deleted
    assert stats["cas_deleted"] == 4
    assert stats["archive_deleted"] == 1

    assert not orphan_cas.exists()
    assert not storage.get_cas_object_path(ws_id, hashes[0]).exists()
    assert not storage.get_cas_object_path(ws_id, hashes[1]).exists()
    assert not storage.get_cas_object_path(ws_id, hashes[2]).exists()
    assert storage.get_cas_object_path(ws_id, hashes[3]).exists()
    assert storage.get_cas_object_path(ws_id, hashes[4]).exists()
    assert not old_arch.exists()
    assert new_arch.exists()


def test_prune_preserves_deleted_file_last_content_version_and_active_version(tmp_path, monkeypatch):
    from app import db
    import time

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage.settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage.settings, "DB_PATH", data_dir / "server_state.db")
    monkeypatch.setattr(storage.settings, "STORAGE_DIR", tmp_path / "storage")

    if hasattr(db._local, "conn"):
        del db._local.conn

    ws_id = "ws1"
    # Upload v1, v2, then delete
    for i in range(2):
        content = f"v{i}".encode()
        h = hashlib.sha256(content).hexdigest()
        src = tmp_path / f"v{i}.txt"
        src.write_bytes(content)
        storage.store_cas_object(ws_id, src, h)
        file_path = storage.get_file_path(ws_id, "del.txt")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        db.upsert_file(ws_id, "del.txt", h, len(content), time.time(), client_name="test")

    rev_del = db.delete_file(ws_id, "del.txt", client_name="test")

    # Prune with keep_versions=1: must keep both the active tombstone (rev 3) AND the last content version (rev 2)!
    stats = storage.prune_storage(keep_versions=1)
    assert stats["versions_deleted"] == 1  # only v0 (rev 1) is deleted
    history = db.get_file_history(ws_id, "del.txt")
    revs = {row["revision"] for row in history}
    assert revs == {2, rev_del}


def test_prune_empty_db_guard_does_not_wipe_cas(tmp_path, monkeypatch):
    from app import db

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage.settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage.settings, "DB_PATH", data_dir / "empty_state.db")
    monkeypatch.setattr(storage.settings, "STORAGE_DIR", tmp_path / "storage")

    if hasattr(db._local, "conn"):
        del db._local.conn

    ws_id = "ws1"
    content = b"important cas object"
    h = hashlib.sha256(content).hexdigest()
    src = tmp_path / "src.txt"
    src.write_bytes(content)
    cas_path = storage.store_cas_object(ws_id, src, h)
    assert cas_path.exists()

    # DB has 0 rows in files and file_versions (e.g. misconfigured DB_PATH)
    stats = storage.prune_storage(keep_versions=1)
    assert stats["cas_deleted"] == 0
    assert cas_path.exists()


def test_archive_file_updates_mtime_for_accurate_expiry(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(storage.settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(storage.settings, "STORAGE_DIR", tmp_path / "storage")

    ws_id = "ws1"
    f_path = storage.get_file_path(ws_id, "ancient.txt")
    f_path.parent.mkdir(parents=True, exist_ok=True)
    f_path.write_bytes(b"ancient content")
    # Set file mtime to 30 days ago before archiving
    ancient_ts = time.time() - 30 * 86400
    os.utime(f_path, (ancient_ts, ancient_ts))

    before_archive = time.time()
    storage.archive_file(ws_id, "ancient.txt")

    archived = storage.list_archive(ws_id)
    assert len(archived) == 1
    # Archive mtime should reflect deletion time, not ancient file mtime
    assert archived[0]["mtime"] >= before_archive - 2


