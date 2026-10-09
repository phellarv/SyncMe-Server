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
