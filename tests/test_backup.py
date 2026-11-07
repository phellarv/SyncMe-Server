import datetime
import hashlib
import io
import json
import os
import stat
import tarfile
import time

import pytest

from app import backup, db, storage
from app.cli import main
from app.config import settings
from app.keys import _write, read_clients

from .conftest import _drop_connection

WS = "ws1"
KEY = "test-key-backup-0123456789abcdef0123456"


def _at(seconds: int) -> datetime.datetime:
    return datetime.datetime(2026, 11, 5, 12, 0, 0, tzinfo=datetime.UTC) + datetime.timedelta(seconds=seconds)


@pytest.fixture
def env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    keys_file = tmp_path / "keys" / "keys.toml"
    monkeypatch.setattr(settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings, "DB_PATH", data_dir / "server_state.db")
    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(settings, "SYNCME_KEYS_FILE", keys_file)
    monkeypatch.setattr(settings, "BACKUP_DIR", None)
    monkeypatch.setattr(settings, "BACKUP_KEEP", 0)
    _write(keys_file, {"laptop": {"key": KEY, "created": "2026-11-01"}})
    dest = tmp_path / "backups"
    dest.mkdir()
    _drop_connection()
    return tmp_path, dest


def _put(path: str, content: bytes, ws: str = WS) -> int:
    """Store a file the way an upload does: CAS object, workspace file, DB row."""
    sha = hashlib.sha256(content).hexdigest()
    target = storage.get_workspace_dir(ws) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    storage.store_cas_object(ws, target, sha)
    row = db.get_file(ws, path)
    expected = row["revision"] if row and not row["is_deleted"] else 0
    return db.upsert_file(ws, path, sha, len(content), target.stat().st_mtime, expected_revision=expected,
                          client_name="laptop")


def _populate():
    _put("notes/todo.txt", b"v1")
    _put("notes/todo.txt", b"v2")
    _put("readme.md", b"hello")
    archive = storage.get_archive_dir(WS) / "old.txt_1700000000"
    archive.write_bytes(b"archived")
    (storage.get_workspace_dir(WS) / ".syncme-upload-abc").write_bytes(b"in-flight upload")


def _members(archive) -> list[str]:
    with tarfile.open(archive) as tar:
        return tar.getnames()


def _stop_server():
    """Close the cached connection the way a stopped server would: the WAL is checkpointed and removed."""
    _drop_connection()


def _wipe_live_state(tmp_path):
    import shutil

    _stop_server()
    shutil.rmtree(tmp_path / "storage")
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / "data" / f"server_state.db{suffix}").unlink(missing_ok=True)


# --- backup ------------------------------------------------------------------------------------------------


def test_backup_writes_a_private_archive_with_db_storage_and_manifest(env):
    tmp_path, dest = env
    _populate()

    archive = backup.create_backup(dest, now=_at(0))

    assert archive.parent == dest
    assert archive.name == "syncme-backup-20261105T120000Z.tar.gz"
    assert backup.NAME_RE.fullmatch(archive.name)
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    names = _members(archive)
    assert "manifest.json" in names
    assert "db/server_state.db" in names
    assert "storage/workspaces/ws1/notes/todo.txt" in names
    assert "storage/workspaces/ws1/readme.md" in names
    assert "storage/archive/ws1/old.txt_1700000000" in names
    sha_v1 = hashlib.sha256(b"v1").hexdigest()
    assert f"storage/objects/ws1/{sha_v1[:2]}/{sha_v1[2:]}" in names     # history survives, not only the live file
    assert not any(n.startswith("keys/") for n in names)                 # keys are opt-in
    assert not any(".syncme-upload-" in n for n in names)               # in-flight uploads are not state
    assert sorted(p.name for p in dest.iterdir()) == [archive.name]     # no temp DB or partial file left


def test_manifest_records_the_workspace_revisions(env):
    _, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))

    manifest = backup.verify_backup(archive)

    assert manifest["format"] == "syncme-backup"
    assert manifest["version"] == 1
    assert manifest["include_keys"] is False
    assert manifest["workspaces"] == {WS: db.get_revision(WS)}


def test_backup_includes_keys_only_when_asked(env):
    _, dest = env
    _populate()
    archive = backup.create_backup(dest, include_keys=True, now=_at(0))

    with tarfile.open(archive) as tar:
        member = tar.getmember("keys/keys.toml")
        assert stat.S_IMODE(member.mode) == 0o600
    assert backup.verify_backup(archive)["include_keys"] is True


def test_backup_fails_and_leaves_nothing_when_a_referenced_cas_object_is_missing(env):
    _, dest = env
    _populate()
    sha_v1 = hashlib.sha256(b"v1").hexdigest()
    storage.get_cas_object_path(WS, sha_v1).unlink()     # e.g. removed by a prune running at the same time

    with pytest.raises(backup.BackupError, match="missing"):
        backup.create_backup(dest, now=_at(0))
    assert list(dest.iterdir()) == []


def test_backup_refuses_when_there_is_no_database_and_does_not_create_one(env):
    tmp_path, dest = env
    with pytest.raises(backup.BackupError, match="database"):
        backup.create_backup(dest, now=_at(0))
    assert not (tmp_path / "data" / "server_state.db").exists()
    assert list(dest.iterdir()) == []


def test_backup_never_overwrites_an_existing_archive(env):
    _, dest = env
    _populate()
    first = backup.create_backup(dest, now=_at(0))
    before = first.read_bytes()

    with pytest.raises(backup.BackupError, match="exists"):
        backup.create_backup(dest, now=_at(0))
    assert first.read_bytes() == before
    assert sorted(p.name for p in dest.iterdir()) == [first.name]


def test_backup_refuses_a_missing_destination(env):
    tmp_path, _ = env
    _populate()
    with pytest.raises(backup.BackupError, match="destination"):
        backup.create_backup(tmp_path / "nope", now=_at(0))


# --- retention ---------------------------------------------------------------------------------------------


def test_retention_keeps_the_newest_n_and_leaves_foreign_files_alone(env):
    _, dest = env
    _populate()
    (dest / "other.tar.gz").write_bytes(b"x")
    (dest / "syncme-backup-notes.txt").write_bytes(b"x")
    made = [backup.create_backup(dest, now=_at(i)) for i in range(3)]

    newest = backup.create_backup(dest, keep=2, now=_at(10))

    remaining = sorted(p.name for p in dest.iterdir())
    assert remaining == sorted(["other.tar.gz", "syncme-backup-notes.txt", made[2].name, newest.name])


def test_retention_zero_keeps_everything(env):
    _, dest = env
    _populate()
    made = [backup.create_backup(dest, keep=0, now=_at(i)) for i in range(3)]
    assert all(p.exists() for p in made)


def test_retention_never_deletes_the_archive_just_written(env):
    _, dest = env
    _populate()
    later = backup.create_backup(dest, now=_at(100))

    earlier = backup.create_backup(dest, keep=1, now=_at(0))     # the clock went backwards

    assert earlier.exists()
    assert not later.exists()


def test_retention_rejects_a_negative_count(env):
    _, dest = env
    _populate()
    with pytest.raises(ValueError):
        backup.create_backup(dest, keep=-1, now=_at(0))
    assert list(dest.iterdir()) == []


# --- verify ------------------------------------------------------------------------------------------------


def _rewrite(archive, out, change):
    """Copy a backup archive member by member; `change(info, data)` returns (info, data) or None to drop it."""
    with tarfile.open(archive) as src, tarfile.open(out, "w:gz") as dst:
        for info in src.getmembers():
            data = src.extractfile(info).read() if info.isfile() else None
            result = change(info, data)
            if result is None:
                continue
            info, data = result
            if data is not None:
                info.size = len(data)
                dst.addfile(info, io.BytesIO(data))
            else:
                dst.addfile(info)
    return out


def test_verify_detects_a_corrupted_cas_object(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))

    def corrupt(info, data):
        if info.name.startswith("storage/objects/") and info.isfile():
            return info, b"tampered"
        return info, data

    bad = _rewrite(archive, tmp_path / "bad.tar.gz", corrupt)
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.verify_backup(bad)


def test_verify_detects_a_missing_referenced_object(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    sha_v1 = hashlib.sha256(b"v1").hexdigest()

    bad = _rewrite(archive, tmp_path / "bad.tar.gz",
                   lambda info, data: None if info.name.endswith(sha_v1[2:]) else (info, data))
    with pytest.raises(backup.BackupError, match="missing"):
        backup.verify_backup(bad)


@pytest.mark.parametrize("evil", ["../evil.txt", "/tmp/evil.txt", "storage/workspaces/../../evil.txt",
                                  "unexpected/file.txt"])
def test_verify_rejects_members_outside_the_known_layout(env, evil):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    out = tmp_path / "bad.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(out, "w:gz") as dst:
        for info in src.getmembers():
            dst.addfile(info, src.extractfile(info) if info.isfile() else None)
        extra = tarfile.TarInfo(evil)
        extra.size = 4
        dst.addfile(extra, io.BytesIO(b"evil"))

    with pytest.raises(backup.BackupError):
        backup.verify_backup(out)


def test_verify_rejects_links(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    out = tmp_path / "link.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(out, "w:gz") as dst:
        for info in src.getmembers():
            dst.addfile(info, src.extractfile(info) if info.isfile() else None)
        link = tarfile.TarInfo("storage/workspaces/ws1/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        dst.addfile(link)

    with pytest.raises(backup.BackupError, match="link"):
        backup.verify_backup(out)


def test_verify_rejects_a_foreign_tarball(env):
    tmp_path, _ = env
    out = tmp_path / "foreign.tar.gz"
    with tarfile.open(out, "w:gz") as dst:
        info = tarfile.TarInfo("hello.txt")
        info.size = 2
        dst.addfile(info, io.BytesIO(b"hi"))
    with pytest.raises(backup.BackupError):
        backup.verify_backup(out)


# --- restore -----------------------------------------------------------------------------------------------


def test_restore_into_an_empty_server_brings_back_files_history_and_archive(env):
    tmp_path, dest = env
    _populate()
    rev_at_backup = db.get_revision(WS)
    archive = backup.create_backup(dest, now=_at(0))
    _wipe_live_state(tmp_path)

    result = backup.restore_backup(archive, now=_at(60))

    assert (storage.get_workspace_dir(WS) / "notes" / "todo.txt").read_bytes() == b"v2"
    assert (storage.get_archive_dir(WS) / "old.txt_1700000000").read_bytes() == b"archived"
    assert db.get_file(WS, "notes/todo.txt")["sha256"] == hashlib.sha256(b"v2").hexdigest()
    history = db.get_file_history(WS, "notes/todo.txt")
    assert [h["sha256"] for h in history] == [hashlib.sha256(b"v2").hexdigest(), hashlib.sha256(b"v1").hexdigest()]
    assert storage.get_cas_object(WS, hashlib.sha256(b"v1").hexdigest()) is not None
    # no live database to compare against: revision numbers jump far ahead so none is ever issued twice
    assert db.get_revision(WS) >= rev_at_backup + backup.REVISION_GAP
    assert result["workspaces"][WS]["backup"] == rev_at_backup


def test_restored_workspace_files_keep_their_mtime_so_indexing_is_a_no_op(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    _wipe_live_state(tmp_path)
    backup.restore_backup(archive, now=_at(60))
    rev = db.get_revision(WS)

    assert db.index_existing_files(storage.get_files_dir()) == 0
    assert db.get_revision(WS) == rev


def test_restore_refuses_to_overwrite_existing_data_without_force(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    _put("readme.md", b"newer than the backup")
    _stop_server()

    with pytest.raises(backup.BackupError, match="--force"):
        backup.restore_backup(archive, now=_at(60))
    assert (storage.get_workspace_dir(WS) / "readme.md").read_bytes() == b"newer than the backup"


def test_restore_with_force_moves_the_old_data_aside_instead_of_deleting_it(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    _put("readme.md", b"newer than the backup")
    _stop_server()

    result = backup.restore_backup(archive, force=True, now=_at(60))

    assert (storage.get_workspace_dir(WS) / "readme.md").read_bytes() == b"hello"
    aside_storage = tmp_path / "storage" / ".pre-restore-20261105T120100Z"
    aside_data = tmp_path / "data" / ".pre-restore-20261105T120100Z"
    assert (aside_storage / "workspaces" / WS / "readme.md").read_bytes() == b"newer than the backup"
    assert (aside_data / "server_state.db").exists()
    assert result["moved_aside"]
    # no leftovers of the staging area
    assert not [p for p in (tmp_path / "storage").iterdir() if p.name.startswith(".restore-")]
    assert not [p for p in (tmp_path / "data").iterdir() if p.name.startswith(".restore-")]


def test_restore_over_a_newer_live_db_never_reissues_a_revision(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    for i in range(5):
        _put("readme.md", f"after backup {i}".encode())
    live_rev = db.get_revision(WS)
    _stop_server()

    result = backup.restore_backup(archive, force=True, now=_at(60))

    assert result["workspaces"][WS]["live"] == live_rev
    assert db.get_revision(WS) >= live_rev
    new_rev = _put("brand-new.txt", b"x")
    assert new_rev > live_rev


def test_restore_keeps_revision_floor_for_workspaces_only_in_the_live_db(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    _put("a.txt", b"x", ws="ws2")
    _put("a.txt", b"y", ws="ws2")
    live_ws2 = db.get_revision("ws2")
    _stop_server()

    backup.restore_backup(archive, force=True, now=_at(60))

    assert db.get_revision("ws2") >= live_ws2


def test_restore_with_keys_roundtrip(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, include_keys=True, now=_at(0))
    _wipe_live_state(tmp_path)
    (tmp_path / "keys" / "keys.toml").unlink()

    backup.restore_backup(archive, include_keys=True, now=_at(60))

    restored = tmp_path / "keys" / "keys.toml"
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    assert read_clients(restored)["laptop"]["key"] == KEY


def test_restore_leaves_keys_alone_unless_asked(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, include_keys=True, now=_at(0))
    _write(tmp_path / "keys" / "keys.toml", {"phone": {"key": KEY, "created": "2026-11-02"}})
    _wipe_live_state(tmp_path)

    backup.restore_backup(archive, now=_at(60))

    assert list(read_clients(tmp_path / "keys" / "keys.toml")) == ["phone"]


def test_restore_with_keys_fails_when_the_backup_has_none(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    _wipe_live_state(tmp_path)

    with pytest.raises(backup.BackupError, match="keys"):
        backup.restore_backup(archive, include_keys=True, now=_at(60))
    assert not (tmp_path / "storage" / "workspaces").exists() or not any((tmp_path / "storage" / "workspaces").iterdir())


def test_restore_rejects_a_tampered_archive_before_touching_live_data(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    out = tmp_path / "evil.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(out, "w:gz") as dst:
        for info in src.getmembers():
            dst.addfile(info, src.extractfile(info) if info.isfile() else None)
        extra = tarfile.TarInfo("storage/workspaces/../../../escaped.txt")
        extra.size = 4
        dst.addfile(extra, io.BytesIO(b"evil"))
    _stop_server()

    with pytest.raises(backup.BackupError):
        backup.restore_backup(out, force=True, now=_at(60))
    assert not (tmp_path / "escaped.txt").exists()
    assert (storage.get_workspace_dir(WS) / "readme.md").read_bytes() == b"hello"
    assert not [p for p in (tmp_path / "storage").iterdir() if p.name.startswith(".pre-restore-")]


# --- CLI ---------------------------------------------------------------------------------------------------


def test_cli_backup_uses_backup_dir_and_keep_from_settings(env, monkeypatch, capsys):
    _, dest = env
    _populate()
    monkeypatch.setattr(settings, "BACKUP_DIR", dest)
    monkeypatch.setattr(settings, "BACKUP_KEEP", 1)

    assert main(["backup"]) == 0
    time.sleep(1.1)                       # archive names have one-second resolution
    assert main(["backup"]) == 0

    archives = [p for p in dest.iterdir() if backup.NAME_RE.fullmatch(p.name)]
    assert len(archives) == 1
    assert str(archives[0]) in capsys.readouterr().out


def test_cli_backup_without_a_destination_is_an_error(env, capsys):
    _populate()
    assert main(["backup"]) == 1
    assert "BACKUP_DIR" in capsys.readouterr().err


def test_cli_backup_error_is_reported_not_raised(env, capsys):
    _, dest = env
    assert main(["backup", "--dest", str(dest)]) == 1       # no database yet
    assert "error:" in capsys.readouterr().err


def test_cli_verify_and_restore(env, capsys):
    tmp_path, dest = env
    _populate()
    assert main(["backup", "--dest", str(dest), "--include-keys"]) == 0
    archive = next(p for p in dest.iterdir() if backup.NAME_RE.fullmatch(p.name))

    assert main(["verify-backup", str(archive)]) == 0
    _stop_server()
    assert main(["restore", str(archive)]) == 1                 # existing data, no --force
    assert "--force" in capsys.readouterr().err
    assert main(["restore", str(archive), "--force"]) == 0
    out = capsys.readouterr().out
    assert "syncme-client init" in out                          # tells the operator what clients must do


def test_cli_restore_output_does_not_print_key_values(env, capsys):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, include_keys=True, now=_at(0))
    _wipe_live_state(tmp_path)
    (tmp_path / "keys" / "keys.toml").unlink()
    assert main(["restore", str(archive), "--include-keys"]) == 0
    captured = capsys.readouterr()
    assert KEY not in captured.out and KEY not in captured.err


def test_archive_permissions_ignore_a_permissive_umask(env):
    _, dest = env
    _populate()
    old = os.umask(0)
    try:
        archive = backup.create_backup(dest, now=_at(0))
    finally:
        os.umask(old)
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_manifest_is_json_inside_the_archive(env):
    _, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["created_at"] == "2026-11-05T12:00:00Z"


# --- review findings ---------------------------------------------------------------------------------------


def test_an_empty_live_db_still_gets_the_revision_gap(env):
    tmp_path, dest = env
    _populate()
    rev_at_backup = db.get_revision(WS)
    archive = backup.create_backup(dest, now=_at(0))
    _wipe_live_state(tmp_path)
    db.get_conn()                       # e.g. `cli status` or a server start creates an empty database
    _stop_server()

    result = backup.restore_backup(archive, force=True, now=_at(60))

    assert db.get_revision(WS) >= rev_at_backup + backup.REVISION_GAP
    assert result["workspaces"][WS]["live"] is None


def test_restore_rewrites_workspace_files_that_disagree_with_the_snapshot(env):
    tmp_path, dest = env
    _populate()
    ws_dir = storage.get_workspace_dir(WS)
    row = db.get_file(WS, "readme.md")
    (ws_dir / "readme.md").unlink()                    # deleted after the snapshot, row still live in it
    (ws_dir / "notes" / "todo.txt").write_bytes(b"torn")   # changed after the snapshot
    archive = backup.create_backup(dest, now=_at(0))
    _wipe_live_state(tmp_path)

    result = backup.restore_backup(archive, now=_at(60))

    assert (ws_dir / "readme.md").read_bytes() == b"hello"
    assert abs((ws_dir / "readme.md").stat().st_mtime - row["mtime"]) < 1e-3
    assert (ws_dir / "notes" / "todo.txt").read_bytes() == b"v2"
    assert result["repaired_files"] == 2


def test_backup_skips_entries_restore_would_reject_instead_of_failing(env, caplog):
    tmp_path, dest = env
    _populate()
    ws_root = tmp_path / "storage" / "workspaces"
    (ws_root / ".DS_Store").write_bytes(b"x")
    (ws_root / "My Documents").mkdir()
    (ws_root / "My Documents" / "a.txt").write_bytes(b"x")
    (storage.get_workspace_dir(WS) / "back\\slash.txt").write_bytes(b"x")
    (tmp_path / "storage" / "archive" / "stray.txt").write_bytes(b"x")

    archive = backup.create_backup(dest, now=_at(0))

    names = _members(archive)
    assert not any("DS_Store" in n or "My Documents" in n or "slash" in n or "stray" in n for n in names)
    assert "skipping" in caplog.text


def test_hardlinked_files_are_stored_as_regular_files(env):
    tmp_path, dest = env
    _populate()
    ws_dir = storage.get_workspace_dir(WS)
    os.link(ws_dir / "readme.md", ws_dir / "readme-copy.md")

    archive = backup.create_backup(dest, now=_at(0))
    _wipe_live_state(tmp_path)
    backup.restore_backup(archive, now=_at(60))

    assert (ws_dir / "readme-copy.md").read_bytes() == b"hello"


def test_restore_refuses_while_the_server_holds_its_lock(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    _stop_server()
    fd = db.lock_server_state(exclusive=False)          # what the running server holds
    try:
        with pytest.raises(backup.BackupError, match="server"):
            backup.restore_backup(archive, force=True, now=_at(60))
    finally:
        os.close(fd)
    assert (storage.get_workspace_dir(WS) / "readme.md").read_bytes() == b"hello"


def test_a_stopped_server_with_a_non_empty_wal_can_be_restored(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    _put("after.txt", b"after the backup")
    # the server never closes its per-thread connections, so a stopped server leaves its WAL behind
    wal = tmp_path / "data" / "server_state.db-wal"
    saved = wal.read_bytes()
    _stop_server()
    wal.write_bytes(saved)

    backup.restore_backup(archive, force=True, now=_at(60))

    assert db.get_file(WS, "after.txt") is None


def test_the_server_takes_the_lock_while_it_runs(env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app):
        with pytest.raises(BlockingIOError):
            os.close(db.lock_server_state(exclusive=True))
    os.close(db.lock_server_state(exclusive=True))      # released on shutdown


def test_a_second_restore_in_the_same_second_never_overwrites_the_first_aside(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    _put("readme.md", b"original live data")
    _stop_server()
    backup.restore_backup(archive, force=True, now=_at(60))
    aside_db = tmp_path / "data" / ".pre-restore-20261105T120100Z" / "server_state.db"
    before = aside_db.read_bytes()

    with pytest.raises(backup.BackupError, match="pre-restore"):
        backup.restore_backup(archive, force=True, now=_at(60))
    assert aside_db.read_bytes() == before
    assert (storage.get_workspace_dir(WS) / "readme.md").read_bytes() == b"hello"


def test_restore_refuses_a_layout_with_nested_targets(env, monkeypatch):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))
    _wipe_live_state(tmp_path)
    monkeypatch.setattr(settings, "ARCHIVE_DIR", tmp_path / "storage" / "workspaces" / "archive")

    with pytest.raises(backup.BackupError, match="inside"):
        backup.restore_backup(archive, now=_at(60))


def test_restored_keys_and_db_are_private_whatever_the_archive_says(env):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, include_keys=True, now=_at(0))

    def loosen(info, data):
        if info.name in (backup.KEYS_MEMBER, backup.DB_MEMBER):
            info.mode = 0o644
        return info, data

    loose = _rewrite(archive, tmp_path / "loose.tar.gz", loosen)
    _wipe_live_state(tmp_path)
    (tmp_path / "keys" / "keys.toml").unlink()

    backup.restore_backup(loose, include_keys=True, now=_at(60))

    assert stat.S_IMODE((tmp_path / "keys" / "keys.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "data" / "server_state.db").stat().st_mode) == 0o600


@pytest.mark.parametrize("workspaces", ["abc", {"ws1": "x"}, {"../x": 1}, {"ws1": -1}, {"ws1": 999}])
def test_verify_rejects_a_manifest_that_does_not_match_the_database(env, workspaces):
    tmp_path, dest = env
    _populate()
    archive = backup.create_backup(dest, now=_at(0))

    def edit(info, data):
        if info.name == backup.MANIFEST:
            manifest = json.loads(data)
            manifest["workspaces"] = workspaces
            data = json.dumps(manifest).encode()
        return info, data

    bad = _rewrite(archive, tmp_path / "bad.tar.gz", edit)
    with pytest.raises(backup.BackupError, match="manifest"):
        backup.verify_backup(bad)


def test_verify_rejects_a_database_with_a_non_numeric_revision(env):
    tmp_path, dest = env
    _populate()
    db.get_conn().execute("UPDATE metadata SET value = 'x' WHERE key = ?", (f"current_revision:{WS}",))
    with pytest.raises(backup.BackupError):
        backup.create_backup(dest, now=_at(0))
    assert list(dest.iterdir()) == []


def test_empty_backup_settings_are_treated_as_unset(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("BACKUP_KEEP", "")
    monkeypatch.setenv("BACKUP_DIR", "")
    s = Settings(_env_file=None)
    assert s.BACKUP_KEEP == 0
    assert s.BACKUP_DIR is None


def test_readme_tells_operators_to_create_the_backup_directory():
    from app.config import PROJECT_ROOT

    readme = (PROJECT_ROOT / "README.md").read_text()
    assert "install -d -o 1000 -g 1000 -m 700 backups" in readme
