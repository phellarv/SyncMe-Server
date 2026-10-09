import pytest
import asyncio
from pathlib import Path
import httpx
import sys

# Add SyncMe-Client to path so we can import the client
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "SyncMe-Client"))

from app.main import app as server_app
from app.db import init_db
from app import storage
from client.api import APIClient

@pytest.fixture
def mock_httpx_client(monkeypatch):
    original_init = httpx.AsyncClient.__init__
    
    def mocked_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=server_app)
        kwargs["base_url"] = "http://testserver"
        original_init(self, *args, **kwargs)
        
    monkeypatch.setattr(httpx.AsyncClient, "__init__", mocked_init)

@pytest.mark.asyncio
async def test_api_client_history_contract(tmp_path, mock_httpx_client, monkeypatch):
    from app.config import settings
    settings.DATA_DIR = tmp_path / "data"
    settings.DATA_DIR.mkdir()
    settings.DB_PATH = None
    settings.STORAGE_DIR = None
    settings.FILES_DIR = None
    settings.ARCHIVE_DIR = None
    
    KEY = "test-key-0123456789abcdef0123456789abcdef"
    import os
    keys_file = tmp_path / "keys.txt"
    keys_file.write_text(f"test-client:{KEY}\n")
    os.environ["SYNCME_KEYS_FILE"] = str(keys_file)
    settings.SYNCME_KEYS_FILE = str(keys_file)
    from app import keys
    keys._keys_cache = None
    
    import app.db as db_mod
    if hasattr(db_mod._local, "conn"):
        del db_mod._local.conn
    db_mod.init_db(db_mod.get_conn())

    client = APIClient("http://testserver", KEY, "client-id", workspace_id="default")
    
    # 1. Upload a file
    await client.upload_file("test.txt", b"v1")
    await client.upload_file("test.txt", b"v2")
    
    # 2. Get history
    history = await client.get_history("test.txt")
    assert "history" in history
    assert len(history["history"]) == 2
    
    # 3. Get revision
    rev1 = history["history"][1]["revision"]
    content = await client.get_history_revision("test.txt", rev1)
    assert content == b"v1"
    
    # 4. Restore history
    restore_res = await client.restore_history("test.txt", rev1)
    assert restore_res["revision"] > rev1
    assert "new_revision" in restore_res or "revision" in restore_res
    
    # Verify restored content
    content_restored = await client.download_file("test.txt")
    assert content_restored == b"v1"
    
    await client.close()
