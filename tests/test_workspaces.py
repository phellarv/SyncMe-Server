import pytest
import shutil
import asyncio
from pathlib import Path
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.config import settings
from app import events

@pytest.fixture(autouse=True)
def setup_teardown():
    settings.DATA_DIR = Path("./test_data")
    if settings.DATA_DIR.exists():
        shutil.rmtree(settings.DATA_DIR)
    settings.DATA_DIR.mkdir(parents=True)
    
    import app.db as db
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
async def test_workspace_isolation(client):
    headers = {"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"}
    
    # Upload to ws1
    res1 = await client.put("/api/v1/workspaces/ws1/files/test.txt", content=b"ws1 content", headers=headers)
    assert res1.status_code == 200
    
    # Upload to ws2
    res2 = await client.put("/api/v1/workspaces/ws2/files/test.txt", content=b"ws2 content", headers=headers)
    assert res2.status_code == 200
    
    # Read from ws1
    read1 = await client.get("/api/v1/workspaces/ws1/files/test.txt", headers=headers)
    assert read1.content == b"ws1 content"
    
    # Read from ws2
    read2 = await client.get("/api/v1/workspaces/ws2/files/test.txt", headers=headers)
    assert read2.content == b"ws2 content"
    
    # Check states are isolated
    state1 = await client.get("/api/v1/workspaces/ws1/sync/state", headers=headers)
    state2 = await client.get("/api/v1/workspaces/ws2/sync/state", headers=headers)
    
    assert state1.json()["latest_revision"] == 1
    assert state2.json()["latest_revision"] == 1
    
    assert state1.json()["files"][0]["size"] == len(b"ws1 content")
    assert state2.json()["files"][0]["size"] == len(b"ws2 content")

@pytest.mark.asyncio
async def test_workspace_events_isolation():
    q1 = events.broadcaster.subscribe("ws1", "client1")
    q2 = events.broadcaster.subscribe("ws2", "client2")
    
    await events.broadcaster.broadcast("ws1", "file_updated", "test.txt", 1)
    
    # q1 should have it
    msg1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    assert msg1["event"] == "file_updated"
    assert msg1["path"] == "test.txt"
    
    # q2 should not have it
    assert q2.empty()
