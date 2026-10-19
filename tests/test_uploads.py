import asyncio
import hashlib
import os
import shutil
import tempfile
import types
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import db, storage
from app.config import settings
from app.main import app

KEY = "test-key-0123456789abcdef0123456789abcdef"
H = {"Authorization": f"Bearer {KEY}"}
UP = "/api/v1/workspaces/ws1/files/"
RESTORE = "/api/v1/workspaces/ws1/archive/restore"
ARCHIVE = "/api/v1/workspaces/ws1/archive"


def ws_files():
    d = settings.DATA_DIR / "files" / "workspaces" / "ws1"
    return sorted(str(p.relative_to(d)) for p in d.rglob("*")) if d.exists() else []


def leftovers():
    return [n for n in ws_files() if os.path.basename(n).startswith(".syncme-upload-")]


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    settings.DATA_DIR = Path("./test_data")
    shutil.rmtree(settings.DATA_DIR, ignore_errors=True)
    settings.DATA_DIR.mkdir(parents=True)
    for name in ("STORAGE_DIR", "FILES_DIR", "ARCHIVE_DIR", "DB_PATH"):
        monkeypatch.setattr(settings, name, None)
    if hasattr(db._local, "conn"):
        del db._local.conn
    yield
    shutil.rmtree(settings.DATA_DIR, ignore_errors=True)


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def state_paths():
    return [r["path"] for r in db.get_state("ws1", 0)[0]]


async def archived_name(client, path="a.txt", content=b"ORIGINAL"):
    await client.put(UP + path, content=content, headers=H)
    await client.delete(UP + path, headers=H)
    return (await client.get(ARCHIVE, headers=H)).json()["files"][0]["archive_filename"]


@pytest.mark.asyncio
async def test_upload_roundtrip_hash_and_size(client):
    body = os.urandom(300_000)
    r = await client.put(UP + "big.bin", content=body, headers=H)
    assert r.status_code == 200
    rec = db.get_file("ws1", "big.bin")
    assert rec["sha256"] == hashlib.sha256(body).hexdigest() and rec["size"] == len(body)
    assert (await client.get(UP + "big.bin", headers=H)).content == body


@pytest.mark.asyncio
async def test_upload_limit_defaults_to_256_mib():
    assert settings.MAX_UPLOAD_BYTES == 256 * 1024 * 1024


@pytest.mark.asyncio
async def test_declared_length_over_limit_is_413_and_leaves_nothing(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)
    r = await client.put(UP + "a.txt", content=b"x" * 2048, headers=H)
    assert r.status_code == 413
    assert ws_files() == [] and state_paths() == []


@pytest.mark.asyncio
async def test_declared_length_over_limit_is_refused_before_a_temp_file_is_created(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)
    created = []
    real = tempfile.NamedTemporaryFile
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", lambda *a, **k: created.append(1) or real(*a, **k))
    r = await client.put(UP + "a.txt", content=b"x" * 2048, headers=H)
    assert r.status_code == 413 and created == []


@pytest.mark.asyncio
async def test_streamed_body_over_limit_is_413_and_leaves_nothing(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)

    async def chunks():                               # no Content-Length: chunked upload
        for _ in range(10):
            yield b"x" * 500

    r = await client.put(UP + "a.txt", content=chunks(), headers=H)
    assert r.status_code == 413
    assert ws_files() == [] and state_paths() == []


@pytest.mark.asyncio
async def test_streamed_body_over_limit_does_not_replace_an_existing_file(client, monkeypatch):
    await client.put(UP + "a.txt", content=b"keep me", headers=H)
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)

    async def chunks():
        for _ in range(10):
            yield b"x" * 500

    assert (await client.put(UP + "a.txt", content=chunks(), headers=H)).status_code == 413
    assert (await client.get(UP + "a.txt", headers=H)).content == b"keep me"
    assert leftovers() == []


@pytest.mark.asyncio
async def test_body_exactly_at_the_limit_is_accepted(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)
    assert (await client.put(UP + "a.txt", content=b"x" * 1024, headers=H)).status_code == 200
    assert (await client.put(UP + "b.txt", content=b"x" * 1025, headers=H)).status_code == 413


@pytest.mark.asyncio
async def test_failed_replace_leaves_no_state_row_and_no_temp_file(client, monkeypatch):
    def boom(src, dst):
        raise OSError("disk exploded")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            await client.put(UP + "a.txt", content=b"x", headers=H)
    assert state_paths() == [] and ws_files() == []
    assert db.get_revision("ws1") == 0                      # the revision counter was rolled back too


@pytest.mark.asyncio
async def test_upload_goes_through_a_reserved_temp_file_in_the_workspace_root(client, monkeypatch):
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (seen.append((str(src), str(dst))), real(src, dst))[1])
    assert (await client.put(UP + "d/a.txt", content=b"x", headers=H)).status_code == 200
    # Filter the replace to the target file
    target_replaces = [s for s in seen if s[1].endswith("/d/a.txt")]
    assert len(target_replaces) == 1
    src, dst = target_replaces[0]
    assert os.path.basename(src).startswith(".syncme-upload-")
    # the workspace root, not the target's directory: that directory does not exist until the commit
    assert os.path.dirname(src) == str(storage.get_workspace_dir("ws1")) and dst.endswith("/d/a.txt")


@pytest.mark.asyncio
async def test_file_in_the_way_of_a_directory_is_409(client):
    await client.put(UP + "a", content=b"x", headers=H)
    r = await client.put(UP + "a/b.txt", content=b"y", headers=H)
    assert r.status_code == 409
    assert state_paths() == ["a"]


@pytest.mark.asyncio
async def test_directory_in_the_way_of_a_file_is_409(client):
    await client.put(UP + "d/x.txt", content=b"x", headers=H)
    r = await client.put(UP + "d", content=b"y", headers=H)
    assert r.status_code == 409
    assert state_paths() == ["d/x.txt"]
    assert leftovers() == []


@pytest.mark.asyncio
async def test_revision_conflict_leaves_the_file_unchanged(client):
    r = await client.put(UP + "a.txt", content=b"one", headers=H)
    r = await client.put(UP + "a.txt", content=b"two", headers={**H, "If-Match": "99"})
    assert r.status_code == 409
    assert (await client.get(UP + "a.txt", headers=H)).content == b"one"
    assert leftovers() == []


@pytest.mark.asyncio
async def test_restore_with_wrong_revision_leaves_target_unchanged(client):
    name = await archived_name(client)
    await client.put(UP + "a.txt", content=b"CURRENT", headers=H)
    target = settings.DATA_DIR / "files" / "workspaces" / "ws1" / "a.txt"
    before = target.read_bytes()
    r = await client.post(
        RESTORE, json={"archive_filename": name, "target_path": "a.txt", "expected_revision": 999}, headers=H
    )
    assert r.status_code == 409
    assert target.read_bytes() == before == b"CURRENT"
    assert leftovers() == []


@pytest.mark.asyncio
async def test_restore_success_still_works(client):
    name = await archived_name(client)
    r = await client.post(RESTORE, json={"archive_filename": name, "target_path": "a.txt"}, headers=H)
    assert r.status_code == 200
    assert (await client.get(UP + "a.txt", headers=H)).content == b"ORIGINAL"
    assert db.get_file("ws1", "a.txt")["sha256"] == hashlib.sha256(b"ORIGINAL").hexdigest()
    assert leftovers() == []


@pytest.mark.asyncio
async def test_restore_goes_through_a_reserved_temp_file_in_the_workspace_root(client, monkeypatch):
    name = await archived_name(client)
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (seen.append((str(src), str(dst))), real(src, dst))[1])
    r = await client.post(RESTORE, json={"archive_filename": name, "target_path": "x/a.txt"}, headers=H)
    assert r.status_code == 200
    (src, dst), = seen
    assert os.path.basename(src).startswith(".syncme-upload-")
    assert os.path.dirname(src) == str(storage.get_workspace_dir("ws1")) and dst.endswith("/x/a.txt")


@pytest.mark.asyncio
async def test_restore_onto_a_file_in_the_way_is_409(client):
    name = await archived_name(client)
    await client.put(UP + "blocker", content=b"x", headers=H)
    r = await client.post(RESTORE, json={"archive_filename": name, "target_path": "blocker/a.txt"}, headers=H)
    assert r.status_code == 409
    assert leftovers() == []


@pytest.mark.asyncio
async def test_restore_onto_a_directory_is_409_and_leaves_no_state_row(client):
    name = await archived_name(client)
    await client.put(UP + "d/x.txt", content=b"x", headers=H)
    r = await client.post(RESTORE, json={"archive_filename": name, "target_path": "d"}, headers=H)
    assert r.status_code == 409
    assert state_paths() == ["a.txt", "d/x.txt"]
    assert leftovers() == []


@pytest.mark.asyncio
async def test_restore_of_a_directory_name_is_404(client):
    await client.put(UP + "d/x.txt", content=b"x", headers=H)
    await client.delete(UP + "d/x.txt", headers=H)
    r = await client.post(RESTORE, json={"archive_filename": "d", "target_path": "z.txt"}, headers=H)
    assert r.status_code == 404


def test_archive_file_refuses_a_name_that_escapes_the_archive(monkeypatch):
    from fastapi import HTTPException

    victim = settings.DATA_DIR / "victim.txt"
    victim.write_bytes(b"x")
    monkeypatch.setattr(storage, "get_file_path", lambda ws, p: victim)   # bypass the earlier guard
    with pytest.raises(HTTPException) as exc:
        storage.archive_file("ws1", "../../escape.txt")
    assert exc.value.status_code == 400
    assert victim.exists()                                                 # nothing was moved
    assert [p for p in settings.DATA_DIR.glob("escape.txt*")] == []


def test_indexer_ignores_upload_temp_files(capsys):
    d = storage.get_workspace_dir("ws1")
    (d / ".syncme-upload-abc").write_bytes(b"half written")
    (d / "real.txt").write_bytes(b"real")
    db.index_existing_files(storage.get_files_dir())
    assert state_paths() == ["real.txt"]
    assert "WARNING" not in capsys.readouterr().out          # skipped silently, not reported as an invalid name


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["n" * 230, "\u00e9" * 115])
async def test_a_name_at_the_limit_can_be_uploaded_and_deleted(client, name):
    # 230 bytes is the longest name that still fits once the archive adds "_<timestamp>-<n>".
    assert len(name.encode()) == 230
    assert (await client.put(UP + name, content=b"x", headers=H)).status_code == 200
    assert (await client.delete(UP + name, headers=H)).status_code == 200


@pytest.mark.asyncio
async def test_a_name_one_byte_over_the_limit_is_rejected(client):
    r = await client.put(UP + "n" * 231, content=b"x", headers=H)
    assert r.status_code == 400
    assert ws_files() == []


@pytest.mark.asyncio
async def test_two_deletes_of_the_same_path_in_one_second_keep_both_archives(client, monkeypatch):
    monkeypatch.setattr(storage, "time", types.SimpleNamespace(time=lambda: 1700000000.0))
    for body in (b"ONE", b"TWO", b"THREE"):
        assert (await client.put(UP + "a.txt", content=body, headers=H)).status_code == 200
        assert (await client.delete(UP + "a.txt", headers=H)).status_code == 200
    d = storage.get_archive_dir("ws1")
    assert {f["archive_filename"] for f in (await client.get(ARCHIVE, headers=H)).json()["files"]} == {
        "a.txt_1700000000", "a.txt_1700000000-1", "a.txt_1700000000-2",
    }
    assert (d / "a.txt_1700000000").read_bytes() == b"ONE"
    assert (d / "a.txt_1700000000-1").read_bytes() == b"TWO"
    assert (d / "a.txt_1700000000-2").read_bytes() == b"THREE"


@pytest.mark.asyncio
async def test_a_long_name_that_was_deleted_can_be_restored(client):
    name = "n" * 225                    # its archive name is 236 bytes: more than a new path may have
    await client.put(UP + name, content=b"LONG", headers=H)
    await client.delete(UP + name, headers=H)
    archived = (await client.get(ARCHIVE, headers=H)).json()["files"][0]["archive_filename"]
    assert len(archived.encode()) > 230
    r = await client.post(RESTORE, json={"archive_filename": archived, "target_path": name}, headers=H)
    assert r.status_code == 200
    assert (await client.get(UP + name, headers=H)).content == b"LONG"


@pytest.mark.asyncio
async def test_the_archive_name_allowance_does_not_apply_to_the_target_path(client):
    archived = await archived_name(client)
    r = await client.post(RESTORE, json={"archive_filename": archived, "target_path": "t" * 231}, headers=H)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_an_archive_name_beyond_the_allowance_is_rejected(client):
    r = await client.post(RESTORE, json={"archive_filename": "x" * 256, "target_path": "a.txt"}, headers=H)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_restore_file_itself_keeps_the_normal_limits_for_the_target_path(client):
    from fastapi import HTTPException

    archived = await archived_name(client)
    with pytest.raises(HTTPException) as exc:
        storage.restore_file("ws1", archived, "t" * 231)
    assert exc.value.status_code == 400


async def _chunks(n=10, size=500):                      # no Content-Length: a chunked upload
    for _ in range(n):
        yield b"x" * size


@pytest.mark.asyncio
async def test_streamed_413_into_a_new_directory_leaves_no_directory_and_no_temp_file(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)
    assert (await client.put(UP + "new/sub/a.txt", content=_chunks(), headers=H)).status_code == 413
    assert ws_files() == []


@pytest.mark.asyncio
async def test_declared_413_into_a_new_directory_leaves_no_directory(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)
    assert (await client.put(UP + "new/sub/a.txt", content=b"x" * 2048, headers=H)).status_code == 413
    assert ws_files() == []


@pytest.mark.asyncio
async def test_revision_conflict_into_a_new_directory_leaves_no_directory(client):
    r = await client.put(UP + "new/sub/a.txt", content=b"x", headers={**H, "If-Match": "5"})
    assert r.status_code == 409
    assert ws_files() == [] and state_paths() == []


@pytest.mark.asyncio
async def test_restore_revision_conflict_into_a_new_directory_leaves_no_directory(client):
    name = await archived_name(client)
    r = await client.post(
        RESTORE, json={"archive_filename": name, "target_path": "n/s/a.txt", "expected_revision": 999}, headers=H
    )
    assert r.status_code == 409
    assert ws_files() == []                              # no "n" directory, no temp file


@pytest.mark.asyncio
async def test_upload_into_a_new_nested_directory_still_works(client):
    assert (await client.put(UP + "p/q/r/a.txt", content=b"deep", headers=H)).status_code == 200
    assert (await client.get(UP + "p/q/r/a.txt", headers=H)).content == b"deep"
    assert leftovers() == []


@pytest.mark.asyncio
async def test_restore_into_a_new_nested_directory_still_works(client):
    name = await archived_name(client)
    r = await client.post(RESTORE, json={"archive_filename": name, "target_path": "p/q/a.txt"}, headers=H)
    assert r.status_code == 200
    assert (await client.get(UP + "p/q/a.txt", headers=H)).content == b"ORIGINAL"
    assert leftovers() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking_path,upload", [("a", "a/b.txt"), ("a", "a/b/c.txt")])
async def test_a_file_in_the_way_is_409_and_rolls_back_the_row_and_the_revision(client, blocking_path, upload):
    assert (await client.put(UP + blocking_path, content=b"x", headers=H)).status_code == 200
    r = await client.put(UP + upload, content=b"y", headers=H)
    assert r.status_code == 409
    assert state_paths() == ["a"] and db.get_revision("ws1") == 1
    assert ws_files() == ["a"]


@pytest.mark.asyncio
async def test_restore_through_a_file_in_the_way_is_409_at_any_depth(client):
    name = await archived_name(client)
    await client.put(UP + "blocker", content=b"x", headers=H)
    for target in ("blocker/a.txt", "blocker/x/a.txt"):
        r = await client.post(RESTORE, json={"archive_filename": name, "target_path": target}, headers=H)
        assert r.status_code == 409, target
    assert state_paths() == ["a.txt", "blocker"] and leftovers() == []


def _record_fsync_and_replace(monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        st = os.fstat(fd)
        events.append(("fsync", st.st_ino, st.st_size))
        return real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", os.stat(src).st_ino, None))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    return events


def _assert_synced_before_replace(events, size):
    replace_event = events[-1]
    assert replace_event[0] == "replace"
    synced = [e for e in events if e[0] == "fsync" and e[1] == replace_event[1]]      # same inode: the temp file
    assert synced, f"the temp file was never fsynced: {events}"
    assert events.index(synced[0]) < events.index(replace_event)
    assert synced[0][2] == size                         # the data was flushed to the file before the fsync


@pytest.mark.asyncio
async def test_upload_is_fsynced_before_it_replaces_the_target(client, monkeypatch):
    events = _record_fsync_and_replace(monkeypatch)
    body = b"payload" * 100
    assert (await client.put(UP + "d/a.txt", content=body, headers=H)).status_code == 200
    _assert_synced_before_replace(events, len(body))


@pytest.mark.asyncio
async def test_restore_is_fsynced_before_it_replaces_the_target(client, monkeypatch):
    name = await archived_name(client, content=b"ORIGINAL" * 50)
    events = _record_fsync_and_replace(monkeypatch)
    r = await client.post(RESTORE, json={"archive_filename": name, "target_path": "x/a.txt"}, headers=H)
    assert r.status_code == 200
    _assert_synced_before_replace(events, len(b"ORIGINAL" * 50))


@pytest.mark.asyncio
async def test_a_content_length_of_non_ascii_digits_is_ignored_not_a_500(raw_asgi):
    # "\u00b2".isdigit() is True but int("\u00b2") raises: the header must be treated as not declared.
    status, _ = await raw_asgi(
        "PUT", UP + "a.txt", {"authorization": f"Bearer {KEY}", "content-length": "\u00b2"}, chunks=[b"hello"]
    )
    assert status == 200
    assert db.get_file("ws1", "a.txt")["size"] == 5


@pytest.mark.asyncio
async def test_upload_persists_in_thread(client, monkeypatch):
    called_in_thread = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        called_in_thread.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)
    r = await client.put(UP + "async_test.txt", content=b"async data", headers=H)
    assert r.status_code == 200
    assert "_persist_upload" in called_in_thread
