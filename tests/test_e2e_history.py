import pytest
from httpx import ASGITransport, AsyncClient
import os
import shutil
from pathlib import Path
import hashlib

from app.config import settings
from app.main import app
from app import storage, db

KEY = "test-key-0123456789abcdef0123456789abcdef"
H = {"Authorization": f"Bearer {KEY}"}
WS = "ws1"
FILE_PATH = "docs/guide.txt"
PUT_URL = f"/api/v1/workspaces/{WS}/files/{FILE_PATH}"
HISTORY_URL = f"/api/v1/workspaces/{WS}/history/{FILE_PATH}"

@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    settings.DATA_DIR = Path("./test_data_e2e_history")
    shutil.rmtree(settings.DATA_DIR, ignore_errors=True)
    settings.DATA_DIR.mkdir(parents=True)
    for name in ("STORAGE_DIR", "FILES_DIR", "ARCHIVE_DIR", "DB_PATH"):
        monkeypatch.setattr(settings, name, None)
    if hasattr(db._local, "conn"):
        del db._local.conn
    
    os.environ["SYNCME_KEYS_FILE"] = str(settings.DATA_DIR / "keys.txt")
    from app.keys import keystore
    keystore.set_keys({"workstation-1": KEY})
    
    yield
    shutil.rmtree(settings.DATA_DIR, ignore_errors=True)

@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")

@pytest.mark.asyncio
async def test_e2e_history_lifecycle(client):
    # 1. Authenticate as client 'workstation-1' (done via headers H and fixture setup)

    # 2. PUT a file docs/guide.txt with content "Version 1" -> revision 1
    r1 = await client.put(PUT_URL, content=b"Version 1", headers=H)
    assert r1.status_code == 200
    rev1 = r1.json()["revision"]

    # 3. PUT the same file with content "Version 2" -> revision 2
    r2 = await client.put(PUT_URL, content=b"Version 2", headers=H)
    assert r2.status_code == 200
    rev2 = r2.json()["revision"]

    # 4. PUT the same file with content "Version 3" -> revision 3
    r3 = await client.put(PUT_URL, content=b"Version 3", headers=H)
    assert r3.status_code == 200
    rev3 = r3.json()["revision"]

    # 5. GET history for docs/guide.txt -> assert 3 versions returned, newest first, correct client and sizes
    r_hist = await client.get(HISTORY_URL, headers=H)
    assert r_hist.status_code == 200
    hist = r_hist.json()["history"]
    assert len(hist) == 3
    assert hist[0]["revision"] == rev3
    assert hist[1]["revision"] == rev2
    assert hist[2]["revision"] == rev1
    for h in hist:
        assert h["client_name"] == "workstation-1"
    assert hist[0]["size"] == len(b"Version 3")
    assert hist[1]["size"] == len(b"Version 2")
    assert hist[2]["size"] == len(b"Version 1")

    # 6. GET content of revision 1 (/revisions/1) -> assert "Version 1"
    r_get1 = await client.get(f"/api/v1/workspaces/{WS}/revisions/{rev1}/{FILE_PATH}", headers=H)
    assert r_get1.status_code == 200
    assert r_get1.content == b"Version 1"

    # 7. GET content of revision 2 (/revisions/2) -> assert "Version 2"
    r_get2 = await client.get(f"/api/v1/workspaces/{WS}/revisions/{rev2}/{FILE_PATH}", headers=H)
    assert r_get2.status_code == 200
    assert r_get2.content == b"Version 2"

    # 8. POST restore revision 1 -> assert returns revision 4. Verify GET /files/docs/guide.txt returns "Version 1".
    body_rest1 = {"restore_revision": rev1}
    r_rest1 = await client.post(f"{HISTORY_URL}/restore", json=body_rest1, headers=H)
    assert r_rest1.status_code == 200
    rev4 = r_rest1.json()["revision"]
    assert rev4 > rev3

    r_curr1 = await client.get(PUT_URL, headers=H)
    assert r_curr1.status_code == 200
    assert r_curr1.content == b"Version 1"

    # 9. DELETE /files/docs/guide.txt -> revision 5 (tombstone).
    r_del = await client.delete(PUT_URL, headers=H)
    assert r_del.status_code == 200
    rev5 = r_del.json()["revision"]
    assert rev5 > rev4

    # 10. GET history -> assert 5 versions, revision 5 is marked is_deleted=True.
    r_hist2 = await client.get(HISTORY_URL, headers=H)
    assert r_hist2.status_code == 200
    hist2 = r_hist2.json()["history"]
    assert len(hist2) == 5
    assert hist2[0]["revision"] == rev5
    assert bool(hist2[0].get("is_deleted")) is True

    # 11. POST restore revision 2 -> assert returns revision 6. Verify GET /files/docs/guide.txt returns "Version 2".
    body_rest2 = {"restore_revision": rev2}
    r_rest2 = await client.post(f"{HISTORY_URL}/restore", json=body_rest2, headers=H)
    assert r_rest2.status_code == 200
    rev6 = r_rest2.json()["revision"]
    assert rev6 > rev5

    r_curr2 = await client.get(PUT_URL, headers=H)
    assert r_curr2.status_code == 200
    assert r_curr2.content == b"Version 2"

    # 12. Verify CAS object deduplication: verify that the number of distinct files in the workspace's CAS object directory matches the number of unique content hashes (not 6!).
    cas_dir = storage.get_cas_dir(WS)
    objects = []
    if cas_dir.exists():
        for root, dirs, files in os.walk(cas_dir):
            for file in files:
                objects.append(os.path.join(root, file))
    
    assert len(objects) == 3

