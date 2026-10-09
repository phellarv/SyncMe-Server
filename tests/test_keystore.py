import json
import os
import threading
from pathlib import Path

import pytest

import app.keys as keys_module
from app.config import settings
from app.keys import KeyFileError, KeyStore, keys_path, read_clients

K1 = "a" * 40
K2 = "b" * 40


def write(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    p = tmp_path / "keys.toml"
    p.write_text(text)
    os.chmod(p, mode)
    return p


def good(extra: str = "") -> str:
    return (
        f'[clients.laptop]\nkey = "{K1}"\ncreated = "2026-10-03"\n\n'
        f'[clients.phone]\nkey = "{K2}"\ncreated = "2026-10-04"\n{extra}'
    )


def test_read_clients_ok(tmp_path):
    p = write(tmp_path, good())
    assert read_clients(p) == {
        "laptop": {"key": K1, "created": "2026-10-03"},
        "phone": {"key": K2, "created": "2026-10-04"},
    }


def test_missing_file(tmp_path):
    with pytest.raises(KeyFileError, match="not found"):
        read_clients(tmp_path / "nope.toml")


def test_empty_file(tmp_path):
    with pytest.raises(KeyFileError, match="no clients"):
        read_clients(write(tmp_path, ""))


def test_clients_table_without_entries_is_rejected(tmp_path):
    with pytest.raises(KeyFileError, match="no clients"):
        read_clients(write(tmp_path, "[clients]\n"))


def test_invalid_toml(tmp_path):
    with pytest.raises(KeyFileError, match="not valid TOML"):
        read_clients(write(tmp_path, "[clients.laptop\nkey = 1"))


def test_short_key_rejected_and_not_echoed(tmp_path):
    short = "s3cr3t-short"
    p = write(tmp_path, f'[clients.laptop]\nkey = "{short}"\n')
    with pytest.raises(KeyFileError) as exc:
        read_clients(p)
    assert "shorter than 32" in str(exc.value)
    assert short not in str(exc.value)


def test_key_of_exactly_32_characters_is_accepted(tmp_path):
    key = "k" * 32
    assert read_clients(write(tmp_path, f'[clients.laptop]\nkey = "{key}"\n'))["laptop"]["key"] == key


def test_key_of_31_characters_is_rejected(tmp_path):
    with pytest.raises(KeyFileError, match="shorter than 32"):
        read_clients(write(tmp_path, f'[clients.laptop]\nkey = "{"k" * 31}"\n'))


def _toml_key_file(tmp_path, key: str) -> Path:
    # json.dumps escapes quotes, backslashes, control characters and non-ASCII; DEL is not escaped by JSON.
    return write(tmp_path, f"[clients.laptop]\nkey = {json.dumps(key).replace(chr(127), '\\u007f')}\n")


@pytest.mark.parametrize(
    "key",
    [
        " " * 40,                                  # whitespace only
        "secret-part one-" + "x" * 30,             # embedded space
        "secret\tpart-" + "x" * 30,                # embedded tab
        "k" * 32 + "\n",                           # trailing newline
        "k" * 31 + "\x20",                         # 32 characters, the last one a space
        "\u00e9" * 40,                             # non-ASCII
        "k" * 31 + "\u00e9",                       # 32 characters, one of them non-ASCII
        "k" * 31 + "\x7f",                         # DEL
    ],
)
def test_key_with_whitespace_or_non_ascii_characters_is_rejected_and_not_echoed(tmp_path, key):
    with pytest.raises(KeyFileError, match="whitespace or non-ASCII") as exc:
        read_clients(_toml_key_file(tmp_path, key))
    assert key.strip() not in str(exc.value) or not key.strip()
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("key", ["k" * 32, "!" * 32, "~" * 32, "a1!~" * 10, "-" * 40])
def test_key_of_printable_ascii_is_accepted_including_identical_characters(tmp_path, key):
    assert read_clients(_toml_key_file(tmp_path, key))["laptop"]["key"] == key


@pytest.mark.parametrize("entry", ['key = 12345678901234567890123456789012345', ""])
def test_missing_or_non_string_key_rejected(tmp_path, entry):
    with pytest.raises(KeyFileError, match="missing or shorter"):
        read_clients(write(tmp_path, f"[clients.laptop]\n{entry}\n"))


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o606, 0o601, 0o610])
def test_group_or_other_access_rejected(tmp_path, mode):
    with pytest.raises(KeyFileError, match="chmod 600"):
        read_clients(write(tmp_path, good(), mode=mode))


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_owner_only_modes_accepted(tmp_path, mode):
    assert set(read_clients(write(tmp_path, good(), mode=mode))) == {"laptop", "phone"}


@pytest.mark.parametrize("name", ["Laptop", "my laptop", "a" * 33, "x_y", "", "-", "-x", "--x", "-" * 32])
def test_invalid_client_name(tmp_path, name):
    p = write(tmp_path, f'[clients."{name}"]\nkey = "{K1}"\n')
    with pytest.raises(KeyFileError, match="invalid client name"):
        read_clients(p)


@pytest.mark.parametrize("name", ["a", "a" * 32, "my-laptop-2", "0", "a-", "9-lives", "a--b"])
def test_valid_client_name(tmp_path, name):
    assert name in read_clients(write(tmp_path, f'[clients."{name}"]\nkey = "{K1}"\n'))


def test_duplicate_key_rejected(tmp_path):
    p = write(tmp_path, f'[clients.a]\nkey = "{K1}"\n[clients.b]\nkey = "{K1}"\n')
    with pytest.raises(KeyFileError, match="share"):
        read_clients(p)


def test_authenticate_returns_name(tmp_path):
    ks = KeyStore()
    ks.load(write(tmp_path, good()))
    assert ks.authenticate(K1) == "laptop"
    assert ks.authenticate(K2) == "phone"
    assert ks.authenticate("c" * 40) is None
    assert ks.authenticate("") is None
    assert ks.authenticate("åäö" * 20) is None   # non-ASCII must not raise


def test_authenticate_compares_against_every_key_without_early_exit(tmp_path, monkeypatch):
    import secrets

    calls = []
    real = secrets.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    ks = KeyStore()
    ks.set_keys({"one": "1" * 40, "two": "2" * 40, "three": "3" * 40})
    monkeypatch.setattr(secrets, "compare_digest", counting)
    assert ks.authenticate("1" * 40) == "one"          # the first entry matches ...
    assert len(calls) == 3                             # ... and all three are still compared


def test_not_loaded_store_rejects_everything():
    assert KeyStore().authenticate(K1) is None


def test_old_api_key_variable_is_a_startup_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SYNCME_API_KEY", "legacy")
    with pytest.raises(KeyFileError, match="no longer supported") as exc:
        KeyStore().load(write(tmp_path, good()))
    assert "legacy" not in str(exc.value)


def test_empty_old_api_key_variable_is_also_a_startup_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SYNCME_API_KEY", "")
    with pytest.raises(KeyFileError, match="no longer supported"):
        KeyStore().load(write(tmp_path, good()))


def test_failed_load_leaves_store_empty(tmp_path):
    ks = KeyStore()
    with pytest.raises(KeyFileError):
        ks.load(tmp_path / "nope.toml")
    assert ks.authenticate(K1) is None


def test_revoked_key_stops_working_after_file_change(tmp_path):
    p = write(tmp_path, good())
    ks = KeyStore()
    ks.load(p)
    assert ks.authenticate(K2) == "phone"
    tmp = tmp_path / "new.toml"
    tmp.write_text(f'[clients.laptop]\nkey = "{K1}"\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)                      # new inode, like the CLI does
    assert ks.authenticate(K2) is None
    assert ks.authenticate(K1) == "laptop"


def test_new_key_is_picked_up_without_restart(tmp_path):
    p = write(tmp_path, f'[clients.laptop]\nkey = "{K1}"\n')
    ks = KeyStore()
    ks.load(p)
    assert ks.authenticate(K2) is None
    tmp = tmp_path / "new.toml"
    tmp.write_text(good())
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    assert ks.authenticate(K2) == "phone"


def test_invalid_reload_keeps_last_valid_set_and_logs(tmp_path, caplog):
    p = write(tmp_path, good())
    ks = KeyStore()
    ks.load(p)
    tmp = tmp_path / "bad.toml"
    tmp.write_text("this is not toml [[[")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    with caplog.at_level("ERROR", logger="syncme.keys"):
        assert ks.authenticate(K1) == "laptop"
        assert ks.authenticate(K1) == "laptop"
    assert "ignoring invalid key file" in caplog.text
    assert caplog.text.count("ignoring invalid key file") == 1   # not once per request
    assert K1 not in caplog.text


def test_deleted_file_keeps_last_valid_set_and_logs_once(tmp_path, caplog):
    p = write(tmp_path, good())
    ks = KeyStore()
    ks.load(p)
    p.unlink()
    with caplog.at_level("ERROR", logger="syncme.keys"):
        assert ks.authenticate(K1) == "laptop"
        assert ks.authenticate(K2) == "phone"
    assert caplog.text.count("ignoring invalid key file") == 1
    assert K1 not in caplog.text


def _same_length_file(tmp_path, key):
    return f'[clients.laptop]\nkey = "{key}"\n'


def test_reload_detects_replacement_with_same_size_and_mtime(tmp_path):
    p = write(tmp_path, _same_length_file(tmp_path, K1))
    ks = KeyStore()
    ks.load(p)
    st = os.stat(p)
    tmp = tmp_path / "new.toml"
    tmp.write_text(_same_length_file(tmp_path, K2))
    os.chmod(tmp, 0o600)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, p)
    assert (os.stat(p).st_size, os.stat(p).st_mtime_ns) == (st.st_size, st.st_mtime_ns)   # only the inode differs
    assert ks.authenticate(K1) is None
    assert ks.authenticate(K2) == "laptop"


def test_reload_detects_in_place_edit_with_same_mtime(tmp_path):
    p = write(tmp_path, _same_length_file(tmp_path, K1))
    ks = KeyStore()
    ks.load(p)
    st = os.stat(p)
    p.write_text(_same_length_file(tmp_path, K2) + "[clients.phone]\nkey = \"" + "c" * 40 + "\"\n")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (os.stat(p).st_ino, os.stat(p).st_mtime_ns) == (st.st_ino, st.st_mtime_ns)       # only the size differs
    assert ks.authenticate("c" * 40) == "phone"


def test_reload_detects_in_place_edit_with_same_size_and_inode(tmp_path):
    p = write(tmp_path, _same_length_file(tmp_path, K1))
    ks = KeyStore()
    ks.load(p)
    st = os.stat(p)
    p.write_text(_same_length_file(tmp_path, K2))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert (os.stat(p).st_ino, os.stat(p).st_size) == (st.st_ino, st.st_size)               # only the mtime differs
    assert ks.authenticate(K1) is None
    assert ks.authenticate(K2) == "laptop"


def test_world_readable_reload_is_rejected(tmp_path):
    p = write(tmp_path, good())
    ks = KeyStore()
    ks.load(p)
    tmp = tmp_path / "loose.toml"
    tmp.write_text(f'[clients.laptop]\nkey = "{K1}"\n[clients.intruder]\nkey = "{"d" * 40}"\n')
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)
    assert ks.authenticate("d" * 40) is None
    assert ks.authenticate(K1) == "laptop"


def test_keys_path_defaults_to_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SYNCME_KEYS_FILE", None)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    assert keys_path() == tmp_path / "keys.toml"
    monkeypatch.setattr(settings, "SYNCME_KEYS_FILE", tmp_path / "x" / "k.toml")
    assert keys_path() == tmp_path / "x" / "k.toml"


def test_set_keys_for_tests():
    ks = KeyStore()
    ks.set_keys({"testclient": K1})
    assert ks.authenticate(K1) == "testclient"
    ks.set_keys({})
    assert ks.authenticate(K1) is None


# --- unreadable key files map to KeyFileError, on startup and on reload --------------------------


def _break_into_directory(p):
    p.unlink()
    p.mkdir()
    os.chmod(p, 0o700)               # 0700, so the group/other check does not fire first


def _break_into_unreadable(p):
    if os.geteuid() == 0:
        pytest.skip("root can read a mode 0200 file")
    p.write_text(good("\n# size changes so that the reload is attempted\n"))
    os.chmod(p, 0o200)


def _break_into_invalid_utf8(p):
    p.write_bytes(b'[clients.laptop]\nkey = "\xff\xfe' + b"x" * 40 + b'"\n')
    os.chmod(p, 0o600)


BREAKERS = {
    "directory": _break_into_directory,
    "unreadable": _break_into_unreadable,
    "invalid-utf8": _break_into_invalid_utf8,
}


@pytest.mark.parametrize("how", BREAKERS)
def test_unreadable_file_is_a_key_file_error_at_startup(tmp_path, how):
    p = write(tmp_path, good())
    BREAKERS[how](p)
    ks = KeyStore()
    with pytest.raises(KeyFileError):
        ks.load(p)
    assert ks.authenticate(K1) is None            # nothing was loaded


@pytest.mark.parametrize("how", BREAKERS)
def test_unreadable_file_on_reload_keeps_the_last_valid_set(tmp_path, caplog, how):
    p = write(tmp_path, good())
    ks = KeyStore()
    ks.load(p)
    BREAKERS[how](p)
    with caplog.at_level("ERROR", logger="syncme.keys"):
        assert ks.authenticate(K1) == "laptop"      # must not raise
        assert ks.authenticate(K2) == "phone"
    assert caplog.text.count("ignoring invalid key file") == 1
    assert K1 not in caplog.text


def test_invalid_utf8_error_does_not_leak_file_bytes(tmp_path):
    p = write(tmp_path, "")
    _break_into_invalid_utf8(p)
    with pytest.raises(KeyFileError, match="UTF-8") as exc:
        read_clients(p)
    assert "codec" not in str(exc.value) and "0xff" not in str(exc.value)


def test_a_parent_that_is_a_regular_file_is_a_key_file_error(tmp_path):
    (tmp_path / "afile").write_text("x")
    with pytest.raises(KeyFileError, match="not accessible"):
        read_clients(tmp_path / "afile" / "keys.toml")


def test_a_directory_that_cannot_be_searched_is_a_key_file_error(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    d = tmp_path / "locked"
    d.mkdir()
    p = d / "keys.toml"
    p.write_text(good())
    os.chmod(p, 0o600)
    os.chmod(d, 0o000)
    try:
        with pytest.raises(KeyFileError, match="not accessible"):
            read_clients(p)
    finally:
        os.chmod(d, 0o700)


# --- thread safety and stamping order --------------------------------------------------------------


def _rewrite(p, text):
    p.write_text(text)             # in place; the size differs between versions, so the stamp changes
    os.chmod(p, 0o600)


V1_KEY = "1" * 40
V2_KEY = "2" * 48


def _v(key):
    return f'[clients.laptop]\nkey = "{key}"\n'


def test_a_slow_reload_cannot_overwrite_a_newer_key_set(tmp_path, monkeypatch):
    p = write(tmp_path, good())
    ks = KeyStore()
    ks.load(p)
    entered, release, calls = threading.Event(), threading.Event(), []
    real = keys_module.read_clients

    def slow_first_read(path):
        data = real(path)                       # the first caller has read the file ...
        if not calls:
            calls.append(1)
            entered.set()
            assert release.wait(10)             # ... and is then held up before it can publish it
        return data

    monkeypatch.setattr(keys_module, "read_clients", slow_first_read)
    _rewrite(p, _v(V1_KEY))
    t1 = threading.Thread(target=ks.authenticate, args=("x",))
    t1.start()
    assert entered.wait(10)
    _rewrite(p, _v(V2_KEY))
    t2 = threading.Thread(target=ks.authenticate, args=("x",))
    t2.start()
    t2.join(0.3)                                # with the lock t2 waits here; without it t2 is already done
    release.set()
    t1.join(10)
    t2.join(10)
    assert not t1.is_alive() and not t2.is_alive()
    assert ks.authenticate(V2_KEY) == "laptop"
    assert ks.authenticate(V1_KEY) is None


def test_load_takes_the_same_lock_as_the_reload(tmp_path):
    p = write(tmp_path, good())
    ks = KeyStore()
    done = threading.Event()

    def load():
        ks.load(p)
        done.set()

    with ks._lock:
        t = threading.Thread(target=load)
        t.start()
        assert not done.wait(0.3), "load() did not wait for the lock"
    t.join(10)
    assert done.is_set() and ks.authenticate(K1) == "laptop"


def test_set_keys_takes_the_same_lock_as_the_reload():
    ks = KeyStore()
    done = threading.Event()

    def install():
        ks.set_keys({"x": "x" * 40})
        done.set()

    with ks._lock:
        t = threading.Thread(target=install)
        t.start()
        assert not done.wait(0.3), "set_keys() did not wait for the lock"
    t.join(10)
    assert done.is_set()


def test_load_stamps_the_file_before_reading_it(tmp_path, monkeypatch):
    p = write(tmp_path, good())
    real = keys_module.read_clients

    def read_then_change_the_file(path):
        data = real(path)
        _rewrite(p, _v(V2_KEY))                 # the file changes after it was read, before load() returns
        return data

    monkeypatch.setattr(keys_module, "read_clients", read_then_change_the_file)
    ks = KeyStore()
    ks.load(p)
    monkeypatch.setattr(keys_module, "read_clients", real)
    assert ks.authenticate(V2_KEY) == "laptop"  # the change is noticed by the next reload
    assert ks.authenticate(K1) is None
