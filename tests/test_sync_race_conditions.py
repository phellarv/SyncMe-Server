import builtins
import hashlib
import shutil
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.db as db
import app.storage as storage
from app.config import settings
from app.main import app

AUTH_HEADERS = {"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"}


@pytest.fixture(autouse=True)
def setup_teardown():
    settings.DATA_DIR = Path("./test_data")
    if settings.DATA_DIR.exists():
        shutil.rmtree(settings.DATA_DIR)
    settings.DATA_DIR.mkdir(parents=True)

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
async def test_delete_file_409_returns_conflict_headers_and_preserves_file_on_db_rollback(client, monkeypatch):
    r1 = await client.put(
        "/api/v1/workspaces/ws1/files/foo.txt",
        content=b"v1",
        headers={**AUTH_HEADERS, "If-Match": "0"},
    )
    assert r1.status_code == 200
    rev1 = r1.json()["revision"]

    r2 = await client.put(
        "/api/v1/workspaces/ws1/files/foo.txt",
        content=b"v2",
        headers={**AUTH_HEADERS, "If-Match": str(rev1)},
    )
    assert r2.status_code == 200
    rev2 = r2.json()["revision"]

    # Stale If-Match on DELETE must return 409 with structured conflict headers
    r_del = await client.delete(
        "/api/v1/workspaces/ws1/files/foo.txt",
        headers={**AUTH_HEADERS, "If-Match": str(rev1)},
    )
    assert r_del.status_code == 409
    assert r_del.headers.get("x-syncme-conflict-reason") == "revision_mismatch"
    assert r_del.headers.get("x-syncme-current-revision") == str(rev2)
    assert r_del.headers.get("x-syncme-head-revision") == str(rev2)
    assert r_del.headers.get("x-syncme-current-sha256") == hashlib.sha256(b"v2").hexdigest()

    # Rollback atomicity: if SQL fails inside delete_file, the active file on disk must not be archived/unlinked
    def _boom_increment(conn, workspace_id):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "_increment_revision", _boom_increment)
    with pytest.raises(RuntimeError, match="simulated DB failure"):
        db.delete_file("ws1", "foo.txt", expected_revision=rev2)

    file_path = storage.get_file_path("ws1", "foo.txt")
    assert file_path.exists(), "Active file was unlinked despite DB transaction rollback!"
    assert file_path.read_bytes() == b"v2"
    assert db.get_file("ws1", "foo.txt")["is_deleted"] == 0


@pytest.mark.asyncio
async def test_put_file_rejected_409_does_not_leave_orphan_cas_object(client):
    r1 = await client.put(
        "/api/v1/workspaces/ws1/files/foo.txt",
        content=b"v1",
        headers={**AUTH_HEADERS, "If-Match": "0"},
    )
    assert r1.status_code == 200

    rejected_payload = b"rejected_v2_payload"
    rejected_sha = hashlib.sha256(rejected_payload).hexdigest()

    r_reject = await client.put(
        "/api/v1/workspaces/ws1/files/foo.txt",
        content=rejected_payload,
        headers={**AUTH_HEADERS, "If-Match": "999"},
    )
    assert r_reject.status_code == 409
    assert storage.get_cas_object("ws1", rejected_sha) is None


def test_prune_storage_does_not_delete_cas_committed_after_candidate_scan(monkeypatch):
    ws_dir = storage.get_workspace_dir("ws1")
    base_file = ws_dir / "base.txt"
    base_bytes = b"base_content"
    base_sha = hashlib.sha256(base_bytes).hexdigest()
    base_file.write_bytes(base_bytes)
    storage.store_cas_object("ws1", base_file, base_sha)
    db.upsert_file("ws1", "base.txt", base_sha, len(base_bytes), time.time(), expected_revision=0)

    # A concurrent upload has written its CAS object to disk just before prune_storage scans cas_base
    concurrent_bytes = b"concurrent_upload_content"
    concurrent_sha = hashlib.sha256(concurrent_bytes).hexdigest()
    tmp_src = ws_dir / "concurrent.txt"
    tmp_src.write_bytes(concurrent_bytes)
    storage.store_cas_object("ws1", tmp_src, concurrent_sha)

    cas_base = storage.get_cas_base_dir()
    orig_iterdir = Path.iterdir
    committed = False

    def _hooked_iterdir(self):
        nonlocal committed
        items = list(orig_iterdir(self))
        if not committed and self == cas_base:
            committed = True
            # Concurrent upload commits its DB row while prune_storage is scanning CAS directories
            db.upsert_file(
                "ws1",
                "concurrent.txt",
                concurrent_sha,
                len(concurrent_bytes),
                time.time(),
                expected_revision=0,
            )
        return iter(items)

    monkeypatch.setattr(Path, "iterdir", _hooked_iterdir)

    storage.prune_storage(keep_versions=10)

    cas_obj = storage.get_cas_object("ws1", concurrent_sha)
    assert cas_obj is not None and cas_obj.exists(), (
        "prune_storage deleted a CAS object that was committed to DB during candidate scan!"
    )


def test_index_existing_files_does_not_clobber_concurrent_live_update(monkeypatch):
    files_dir = storage.get_files_dir()
    ws1_dir = files_dir / "workspaces" / "ws1"
    ws1_dir.mkdir(parents=True, exist_ok=True)

    race_file = ws1_dir / "race.txt"
    stale_bytes = b"A" * 70000  # > 65536 so index_existing_files uses open(p, "rb") outside the DB transaction
    race_file.write_bytes(stale_bytes)

    live_bytes = b"live_v2_committed_concurrently"
    live_sha = hashlib.sha256(live_bytes).hexdigest()

    orig_open = builtins.open
    interleaved = False

    def _hooked_open(file, *args, **kwargs):
        nonlocal interleaved
        f = orig_open(file, *args, **kwargs)
        if not interleaved and Path(file) == race_file:
            interleaved = True
            # Read happens first; wrap close() so right as index_existing_files finishes reading race_file,
            # a concurrent live upload commits rev 1 to DB and replaces race_file on disk.
            orig_close = f.close

            def _close_and_interleave():
                orig_close()
                new_mtime = time.time() + 10.0
                race_file.write_bytes(live_bytes)
                storage.store_cas_object("ws1", race_file, live_sha)
                db.upsert_file("ws1", "race.txt", live_sha, len(live_bytes), new_mtime, expected_revision=0)

            f.close = _close_and_interleave
        return f

    monkeypatch.setattr(builtins, "open", _hooked_open)

    db.index_existing_files(files_dir)

    rec = db.get_file("ws1", "race.txt")
    assert rec is not None
    assert rec["sha256"] == live_sha
    assert rec["revision"] == 1
