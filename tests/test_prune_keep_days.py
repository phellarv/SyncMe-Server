"""`prune --keep-days N`: every state of the last N days stays restorable, older history goes."""
import hashlib
import time

import pytest

from app import db, storage
from app.cli import main
from app.config import PROJECT_ROOT, settings

WS = "ws1"
DAY = 86400.0


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings, "DB_PATH", data_dir / "server_state.db")
    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path / "storage")
    db.close_conn()
    yield
    db.close_conn()


def _put(path: str, content: bytes, days_ago: float) -> int:
    sha = hashlib.sha256(content).hexdigest()
    target = storage.get_workspace_dir(WS) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    storage.store_cas_object(WS, target, sha)
    rev = db.upsert_file(WS, path, sha, len(content), time.time(), client_name="t")
    _age(rev, days_ago)
    return rev


def _delete(path: str, days_ago: float) -> int:
    rev = db.delete_file(WS, path, client_name="t")
    _age(rev, days_ago)
    return rev


def _age(rev: int, days_ago: float) -> None:
    db.get_conn().execute("UPDATE file_versions SET created_at = ? WHERE workspace_id = ? AND revision = ?",
                          (time.time() - days_ago * DAY, WS, rev))


def _revisions(path: str) -> list[int]:
    return sorted(v["revision"] for v in db.get_file_history(WS, path, limit=1000))


def _cas(content: bytes):
    return storage.get_cas_object_path(WS, hashlib.sha256(content).hexdigest())


def test_keeps_everything_newer_than_the_cutoff_plus_the_state_at_the_cutoff():
    r30 = _put("doc.txt", b"30 days", 30)
    r20 = _put("doc.txt", b"20 days", 20)
    r10 = _put("doc.txt", b"10 days", 10)
    r1 = _put("doc.txt", b"1 day", 1)

    stats = storage.prune_storage(keep_days=14)

    # 20 days is the newest version before the cutoff: the state 14 days ago, so the 10-day edit can be undone
    assert _revisions("doc.txt") == [r20, r10, r1]
    assert stats["versions_deleted"] == 1
    assert not _cas(b"30 days").exists()
    assert _cas(b"20 days").exists() and _cas(b"10 days").exists() and _cas(b"1 day").exists()
    assert r30 not in _revisions("doc.txt")


def test_many_recent_edits_are_all_kept():
    revs = [_put("busy.txt", f"edit {i}".encode(), 13 - i * 0.5) for i in range(25)]
    storage.prune_storage(keep_days=14)
    assert _revisions("busy.txt") == revs


def test_a_file_untouched_for_months_keeps_its_current_version():
    _put("old.txt", b"ancient", 200)
    r = _put("old.txt", b"still current", 100)
    storage.prune_storage(keep_days=14)
    assert _revisions("old.txt") == [r]
    assert db.get_file(WS, "old.txt")["revision"] == r
    assert _cas(b"still current").exists()


def test_a_recently_deleted_file_can_still_be_restored():
    _put("gone.txt", b"older content", 60)
    content_rev = _put("gone.txt", b"last content", 40)
    tomb = _delete("gone.txt", 3)

    storage.prune_storage(keep_days=14)

    assert _revisions("gone.txt") == [content_rev, tomb]
    assert _cas(b"last content").exists()


def test_a_file_deleted_long_ago_loses_its_content_history():
    _put("ancient.txt", b"content", 60)
    tomb = _delete("ancient.txt", 30)

    storage.prune_storage(keep_days=14)

    assert _revisions("ancient.txt") == [tomb]
    assert not _cas(b"content").exists()


def test_keep_days_replaces_the_version_count():
    for i in range(12):
        _put("doc.txt", f"v{i}".encode(), 30 - i)      # 30..19 days ago, all before the cutoff
    storage.prune_storage(keep_days=14)
    assert len(_revisions("doc.txt")) == 1               # not the default 10


def test_count_rule_is_unchanged_without_keep_days():
    for i in range(12):
        _put("doc.txt", f"v{i}".encode(), 30 - i)
    storage.prune_storage(keep_versions=10)
    assert len(_revisions("doc.txt")) == 10


def test_keep_days_must_not_be_negative():
    with pytest.raises(ValueError):
        storage.prune_storage(keep_days=-1)


def test_cli_prune_keep_days(capsys):
    _put("doc.txt", b"30 days", 30)
    _put("doc.txt", b"20 days", 20)
    _put("doc.txt", b"1 day", 1)

    assert main(["prune", "--keep-days", "14", "--archive-days", "14"]) == 0
    assert "Pruned 1 old version record(s)" in capsys.readouterr().out


def test_cli_prune_keep_days_validation(capsys):
    assert main(["prune", "--keep-days", "-1"]) == 1
    assert "--keep-days" in capsys.readouterr().err


def test_cli_prune_rejects_both_retention_rules():
    with pytest.raises(SystemExit) as exc:
        main(["prune", "--keep-days", "14", "--keep-versions", "3"])
    assert exc.value.code == 2


def test_systemd_backup_unit_prunes_before_the_backup():
    service = (PROJECT_ROOT / "systemd" / "syncme-backup.service").read_text()
    lines = [line for line in service.splitlines() if line.startswith("Exec")]
    # --build: `run` reuses an existing image, so without it a `git pull` would leave the tool image on old code
    assert lines[0] == ("ExecStartPre=-/usr/bin/docker compose run --build --rm backup prune --keep-days 14 "
                        "--archive-days 14")
    assert lines[1] == "ExecStart=/usr/bin/docker compose run --build --rm backup backup"


def test_readme_documents_keep_days():
    readme = (PROJECT_ROOT / "README.md").read_text()
    assert "--keep-days" in readme
    assert "prune --keep-days 14 --archive-days 14" in readme
