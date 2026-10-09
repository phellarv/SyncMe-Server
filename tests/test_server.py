import pytest
import asyncio
import os
import shutil
from pathlib import Path
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.config import settings

@pytest.fixture(autouse=True)
def setup_teardown():
    # Setup
    settings.DATA_DIR = Path("./test_data")
    if settings.DATA_DIR.exists():
        shutil.rmtree(settings.DATA_DIR)
    settings.DATA_DIR.mkdir(parents=True)
    
    # Import threading local after setting path
    import app.db as db
    if hasattr(db._local, "conn"):
        del db._local.conn
    
    yield
    
    # Teardown
    if settings.DATA_DIR.exists():
        shutil.rmtree(settings.DATA_DIR)

@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")

@pytest.mark.asyncio
async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_api_v1_health(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_health_db_failure(client, monkeypatch):
    import app.db as db

    def mock_get_conn():
        raise Exception("DB connection error")

    monkeypatch.setattr(db, "get_conn", mock_get_conn)
    response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["detail"] == "Database unavailable"
    assert "DB connection error" not in response.text

@pytest.mark.asyncio
async def test_auth(client):
    response = await client.get("/api/v1/workspaces/ws1/sync/state")
    assert response.status_code == 401

    response = await client.get("/api/v1/workspaces/ws1/sync/state", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401

    response = await client.get("/api/v1/workspaces/ws1/sync/state", headers={"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"})
    assert response.status_code == 200

@pytest.mark.asyncio
async def test_upload_file(client):
    content = b"hello world"
    headers = {"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"}
    
    # Upload
    response = await client.put("/api/v1/workspaces/ws1/files/test.txt", content=content, headers=headers)
    assert response.status_code == 200
    rev1 = response.json()["revision"]
    assert rev1 > 0
    
    # Get state
    response = await client.get("/api/v1/workspaces/ws1/sync/state", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["files"]) == 1
    assert data["files"][0]["path"] == "test.txt"
    assert data["files"][0]["size"] == len(content)
    assert data["latest_revision"] == rev1
    assert data["last_revision"] == rev1
    
    # Get file
    response = await client.get("/api/v1/workspaces/ws1/files/test.txt", headers=headers)
    assert response.status_code == 200
    assert response.content == content

@pytest.mark.asyncio
async def test_optimistic_locking(client):
    content1 = b"hello"
    content2 = b"world"
    headers = {"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"}
    
    response = await client.put("/api/v1/workspaces/ws1/files/lock.txt", content=content1, headers=headers)
    rev1 = response.json()["revision"]
    
    headers_with_match = {**headers, "If-Match": str(rev1)}
    response = await client.put("/api/v1/workspaces/ws1/files/lock.txt", content=content2, headers=headers_with_match)
    assert response.status_code == 200
    rev2 = response.json()["revision"]
    
    headers_bad_match = {**headers, "If-Match": str(rev1)}
    response = await client.put("/api/v1/workspaces/ws1/files/lock.txt", content=b"fail", headers=headers_bad_match)
    assert response.status_code == 409

@pytest.mark.asyncio
async def test_delete_and_archive(client):
    content = b"delete me"
    headers = {"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"}
    
    await client.put("/api/v1/workspaces/ws1/files/del.txt", content=content, headers=headers)
    
    response = await client.delete("/api/v1/workspaces/ws1/files/del.txt", headers=headers)
    assert response.status_code == 200
    
    response = await client.get("/api/v1/workspaces/ws1/files/del.txt", headers=headers)
    assert response.status_code == 404
    
    response = await client.get("/api/v1/workspaces/ws1/archive", headers=headers)
    data = response.json()
    assert len(data["files"]) == 1
    assert "del.txt" in data["files"][0]["archive_filename"]

@pytest.mark.asyncio
async def test_sse_events(client):
    import asyncio
    headers = {"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"}
    pass

@pytest.mark.asyncio
async def test_index_existing_files(client):
    from app import storage, db
    
    # Create file manually on disk
    test_content = b"indexed content"
    files_dir = storage.get_files_dir()
    ws_dir = files_dir / "workspaces" / "ws1"
    ws_dir.mkdir(parents=True)
    test_file_path = ws_dir / "pre_existing.txt"
    test_file_path.write_bytes(test_content)
    
    # Index it (simulating startup)
    db.index_existing_files(files_dir)
    
    # Check if registered
    response = await client.get("/api/v1/workspaces/ws1/sync/state", headers={"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"})
    assert response.status_code == 200
    data = response.json()
    assert any(f["path"] == "pre_existing.txt" for f in data["files"])
