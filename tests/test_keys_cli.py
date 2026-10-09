import fcntl
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

import app.keys as keys_module
from app.config import PROJECT_ROOT, settings
from app.keys import KeyFileError, KeyStore, main, read_clients


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    p = tmp_path / "keys" / "keys.toml"
    monkeypatch.setattr(settings, "SYNCME_KEYS_FILE", p)
    return p


def test_add_creates_file_prints_key_once_and_never_logs(keyfile, capsys, caplog):
    with caplog.at_level("DEBUG"):
        assert main(["add", "laptop"]) == 0
    out, err = capsys.readouterr()
    key = out.strip()
    assert len(key) >= 32 and err == ""
    assert read_clients(keyfile)["laptop"]["key"] == key
    assert key not in caplog.text
    assert stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600
    ks = KeyStore()
    ks.load(keyfile)
    assert ks.authenticate(key) == "laptop"


def test_add_second_client_keeps_first(keyfile, capsys):
    main(["add", "laptop"])
    k1 = capsys.readouterr().out.strip()
    main(["add", "phone"])
    k2 = capsys.readouterr().out.strip()
    clients = read_clients(keyfile)
    assert clients["laptop"]["key"] == k1 and clients["phone"]["key"] == k2 and k1 != k2


@pytest.mark.parametrize("name", ["Laptop", "a b", "x" * 33, "a_b", "../x", "", "-", "-x", "--x", "-" * 32])
def test_add_rejects_bad_names(keyfile, capsys, name):
    assert main(["add", "--", name]) == 1          # "--": argparse must not read the name as an option
    assert "invalid client name" in capsys.readouterr().err
    assert not keyfile.exists()


@pytest.mark.parametrize("name", ["a-", "0", "9-lives", "a" * 32])
def test_add_accepts_names_that_do_not_start_with_a_hyphen(keyfile, capsys, name):
    assert main(["add", "--", name]) == 0
    assert name in read_clients(keyfile)


def test_add_rejects_existing_name(keyfile, capsys):
    main(["add", "laptop"])
    capsys.readouterr()
    assert main(["add", "laptop"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert len(read_clients(keyfile)) == 1


def test_add_refuses_to_touch_an_invalid_file(keyfile, capsys):
    keyfile.parent.mkdir(parents=True)
    keyfile.write_text("not toml [[[")
    os.chmod(keyfile, 0o600)
    before = keyfile.read_text()
    assert main(["add", "laptop"]) == 1
    assert keyfile.read_text() == before


def test_add_refuses_to_touch_a_file_with_loose_permissions(keyfile, capsys):
    main(["add", "laptop"])
    capsys.readouterr()
    os.chmod(keyfile, 0o644)
    before = keyfile.read_text()
    assert main(["add", "phone"]) == 1
    assert "chmod 600" in capsys.readouterr().err
    assert keyfile.read_text() == before


def test_rewrite_preserves_keys_with_characters_that_need_escaping(keyfile, capsys):
    odd_key = 'q"uote\\back-slash!' + "x" * 30
    odd_created = 'on "day" \\ 1\t' + "é" * 5
    keyfile.parent.mkdir(parents=True)
    keyfile.write_text(f"[clients.old]\nkey = '{odd_key}'\ncreated = '{odd_created}'\n")   # TOML literal strings
    os.chmod(keyfile, 0o600)
    assert main(["add", "new"]) == 0
    clients = read_clients(keyfile)          # the rewritten file must still be valid TOML
    assert clients["old"] == {"key": odd_key, "created": odd_created}
    assert clients["new"]["key"] == capsys.readouterr().out.strip()


def test_rewrite_preserves_a_value_containing_the_delete_character(keyfile, capsys):
    # DEL is not a valid key character any more, but `created` is free-form and must survive a rewrite.
    keyfile.parent.mkdir(parents=True)
    keyfile.write_text('[clients.old]\nkey = "' + "x" * 40 + '"\ncreated = "del\\u007f-x"\n')   # escaped in a basic string
    os.chmod(keyfile, 0o600)
    assert main(["add", "new"]) == 0
    assert read_clients(keyfile)["old"]["created"] == "del\x7f-x"


def test_list_shows_names_and_dates_but_no_keys(keyfile, capsys):
    main(["add", "laptop"])
    key = capsys.readouterr().out.strip()
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "laptop" in out and key not in out


def test_list_without_a_key_file_is_an_error(keyfile, capsys):
    assert main(["list"]) == 1
    assert "not found" in capsys.readouterr().err


def test_revoke_removes_client(keyfile, capsys):
    main(["add", "laptop"])
    main(["add", "phone"])
    capsys.readouterr()
    assert main(["revoke", "phone"]) == 0
    assert list(read_clients(keyfile)) == ["laptop"]
    assert stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600


def test_revoke_refuses_last_client(keyfile, capsys):
    main(["add", "laptop"])
    capsys.readouterr()
    assert main(["revoke", "laptop"]) == 1
    assert "last" in capsys.readouterr().err
    assert list(read_clients(keyfile)) == ["laptop"]


def test_revoke_unknown_name(keyfile, capsys):
    main(["add", "laptop"])
    capsys.readouterr()
    assert main(["revoke", "ghost"]) == 1
    assert "not found" in capsys.readouterr().err


def test_no_temp_files_left_behind(keyfile):
    main(["add", "laptop"])
    main(["add", "phone"])
    main(["revoke", "phone"])
    assert sorted(p.name for p in keyfile.parent.iterdir()) == [".keys.lock", "keys.toml"]


def test_atomic_write_failure_leaves_original_intact(keyfile, monkeypatch, capsys):
    main(["add", "laptop"])
    capsys.readouterr()
    before = keyfile.read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        main(["add", "phone"])
    assert keyfile.read_text() == before
    assert sorted(p.name for p in keyfile.parent.iterdir()) == [".keys.lock", "keys.toml"]


# --- the temp file is flushed to disk before it replaces the key file ----------------------------


def test_temp_file_is_fsynced_with_its_content_before_the_rename(keyfile, monkeypatch):
    calls = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        calls.append(("fsync", os.fstat(fd).st_size))     # size > 0: the data was flushed to the fd first
        return real_fsync(fd)

    def replace(src, dst):
        calls.append(("replace", None))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    assert main(["add", "laptop"]) == 0
    assert [c[0] for c in calls] == ["fsync", "replace"]
    assert calls[0][1] > 0


# --- concurrent add/revoke must neither lose entries nor resurrect a revoked key -----------------


@pytest.fixture
def slow_write(monkeypatch):
    """Widen the window between reading the key file and writing it back, so that races are certain."""
    real = keys_module._write

    def slow(path, clients):
        time.sleep(0.05)
        real(path, clients)

    monkeypatch.setattr(keys_module, "_write", slow)


def _run_concurrently(argvs):
    results, barrier = {}, threading.Barrier(len(argvs))

    def work(i, argv):
        barrier.wait(10)
        results[i] = main(argv)

    threads = [threading.Thread(target=work, args=(i, a)) for i, a in enumerate(argvs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive()
    return [results[i] for i in range(len(argvs))]


def test_concurrent_adds_all_survive(keyfile, capsys, slow_write):
    names = [f"client-{i}" for i in range(8)]
    assert _run_concurrently([["add", n] for n in names]) == [0] * len(names)
    clients = read_clients(keyfile)
    assert sorted(clients) == sorted(names)
    printed = set(capsys.readouterr().out.split())
    assert printed == {e["key"] for e in clients.values()}     # every key shown to an operator is in the file


def test_concurrent_add_and_revoke_neither_lose_the_add_nor_resurrect_the_key(keyfile, capsys, slow_write):
    main(["add", "alpha"])
    main(["add", "beta"])
    capsys.readouterr()
    assert _run_concurrently([["revoke", "beta"], ["add", "gamma"]]) == [0, 0]
    assert sorted(read_clients(keyfile)) == ["alpha", "gamma"]


_SLOW_CLI = """
import sys, time
import app.keys as k
real = k._write
def slow(path, clients):
    time.sleep(0.5)
    real(path, clients)
k._write = slow
sys.exit(k.main(sys.argv[1:]))
"""


def test_concurrent_adds_from_separate_processes_all_survive(keyfile):
    env = {**os.environ, "SYNCME_KEYS_FILE": str(keyfile)}
    names = ["proc-a", "proc-b", "proc-c"]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _SLOW_CLI, "add", n],
            cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for n in names
    ]
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], outs
    assert sorted(read_clients(keyfile)) == names


def _hold_lock(keyfile):
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(keyfile.parent / ".keys.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


@pytest.mark.parametrize("argv", [["add", "newcomer"], ["revoke", "beta"]])
def test_add_and_revoke_wait_for_the_lock_before_reading_or_writing(keyfile, capsys, argv):
    main(["add", "alpha"])
    main(["add", "beta"])
    capsys.readouterr()
    before = keyfile.read_text()
    fd = _hold_lock(keyfile)
    result = []
    t = threading.Thread(target=lambda: result.append(main(argv)))
    try:
        t.start()
        t.join(0.3)
        assert t.is_alive(), "the command did not wait for the lock"
        assert keyfile.read_text() == before
    finally:
        os.close(fd)                      # releases the lock
        t.join(10)
    assert result == [0]
    assert keyfile.read_text() != before


def test_lock_file_is_private_and_lives_next_to_the_key_file(keyfile):
    main(["add", "alpha"])
    lock = keyfile.parent / ".keys.lock"
    assert lock.exists() and stat.S_IMODE(os.stat(lock).st_mode) == 0o600


def test_revoke_in_a_missing_directory_is_an_error_and_creates_nothing(keyfile, capsys):
    assert not keyfile.parent.exists()
    assert main(["revoke", "alpha"]) == 1
    assert "not found" in capsys.readouterr().err
    assert not keyfile.parent.exists()


def test_a_lock_file_that_cannot_be_created_is_an_error_not_a_traceback(keyfile, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    main(["add", "alpha"])
    capsys.readouterr()
    (keyfile.parent / ".keys.lock").unlink()
    os.chmod(keyfile.parent, 0o500)       # no write permission: the lock file cannot be created
    try:
        assert main(["add", "other"]) == 1
        assert "lock file" in capsys.readouterr().err
    finally:
        os.chmod(keyfile.parent, 0o700)
