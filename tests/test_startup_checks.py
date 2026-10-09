import os
import time

import pytest

import app.main as main_module
from app import db, storage
from app.config import settings
from app.keys import main as keys_main




@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(settings, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(settings, "FILES_DIR", None)
    monkeypatch.setattr(settings, "DB_PATH", None)
    return tmp_path




@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_key_cli_explains_an_unwritable_key_directory(tmp_path, monkeypatch, capsys):
    kd = tmp_path / "keys"
    kd.mkdir()
    os.chmod(kd, 0o500)
    monkeypatch.setattr(settings, "SYNCME_KEYS_FILE", kd / "keys.toml")
    try:
        assert keys_main(["add", "laptop"]) == 1
    finally:
        os.chmod(kd, 0o700)
    err = capsys.readouterr().err
    assert "writable by uid" in err and "KEYS_UID" in err and "Traceback" not in err





def _make(path, age_seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    when = time.time() - age_seconds
    os.utime(path, (when, when))
    return path


def _keyfile(dirs, monkeypatch):
    monkeypatch.setattr(main_module, "configure_logging", lambda: None)
    keyfile = dirs / "keys.toml"
    keyfile.write_text(f'[clients.fromfile]\nkey = "{"f" * 40}"\n')
    os.chmod(keyfile, 0o600)
    monkeypatch.setattr(settings, "SYNCME_KEYS_FILE", keyfile)


# --- the startup sweep of orphaned upload temp files -------------------------------------------------

def test_sweep_removes_old_upload_temp_files_and_nothing_else(dirs):
    ws = dirs / "storage" / "workspaces"
    old = _make(ws / "ws1" / ".syncme-upload-old", 2 * 3600)
    nested = _make(ws / "ws1" / "a" / "b" / ".syncme-upload-nested", 3700)
    other_ws = _make(ws / "ws2" / ".syncme-upload-other", 5 * 3600)
    keep = [
        _make(ws / "ws1" / ".syncme-upload-young", 3500),                # in flight in another process, maybe
        _make(ws / "ws1" / ".syncme-upload-brand-new", 5),
        _make(ws / "ws1" / "report.syncme-upload-copy", 2 * 3600),         # the prefix only in the middle
        _make(ws / "ws1" / "a" / "x.syncme-upload-", 2 * 3600),
        _make(ws / "ws1" / "a.txt", 2 * 3600),                             # an ordinary old file
        _make(ws / "ws1" / "a" / "b" / "c.txt", 2 * 3600),
    ]
    assert storage.sweep_stale_temp_files() == 3
    assert not old.exists() and not nested.exists() and not other_ws.exists()
    assert all(p.exists() for p in keep)
    assert (ws / "ws1" / "a" / "b").is_dir() and (ws / "ws2").is_dir()    # directories are left alone


def test_sweep_without_any_workspace_directory_is_a_no_op(dirs):
    assert storage.sweep_stale_temp_files() == 0


def test_sweep_survives_a_file_that_vanishes_underneath_it(dirs, monkeypatch):
    ws = dirs / "storage" / "workspaces" / "ws1"
    gone = _make(ws / ".syncme-upload-gone", 2 * 3600)
    other = _make(ws / ".syncme-upload-other", 2 * 3600)
    real = os.lstat

    def lstat(path, *a, **k):
        if os.path.basename(path) == gone.name:
            raise FileNotFoundError(path)                  # another process removed it in the meantime
        return real(path, *a, **k)

    monkeypatch.setattr(os, "lstat", lstat)
    assert storage.sweep_stale_temp_files() == 1
    assert not other.exists()


def test_sweep_does_not_follow_symlinks_out_of_the_workspaces(dirs, tmp_path):
    outside = _make(tmp_path / "outside" / ".syncme-upload-victim", 5 * 3600)
    ws = dirs / "storage" / "workspaces" / "ws1"
    ws.mkdir(parents=True)
    (ws / "link").symlink_to(outside.parent)
    assert storage.sweep_stale_temp_files() == 0
    assert outside.exists()


@pytest.mark.asyncio
async def test_startup_sweeps_before_indexing(dirs, monkeypatch):
    _keyfile(dirs, monkeypatch)
    ws = dirs / "storage" / "workspaces" / "ws1"
    old = _make(ws / ".syncme-upload-old", 2 * 3600)
    young = _make(ws / ".syncme-upload-young", 60)
    order = []

    def traced(module, name, label, check=None):
        real = getattr(module, name)

        def wrapper(*a, **k):
            order.append(label)
            if check:
                check()
            return real(*a, **k)

        monkeypatch.setattr(module, name, wrapper)

    traced(storage, "sweep_stale_temp_files", "sweep")
    traced(db, "index_existing_files", "index", check=lambda: order.append(f"old-exists={old.exists()}"))
    async with main_module.app.router.lifespan_context(main_module.app):
        pass
    assert order == ["sweep", "index", "old-exists=False"]
    assert young.exists()


