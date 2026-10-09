import argparse
import contextlib
import datetime
import fcntl
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")      # no leading hyphen: it would read as a CLI option
MIN_KEY_LEN = 32
# Printable ASCII without whitespace, so that a key can always be sent in an HTTP header.
KEY_RE = re.compile(rf"[\x21-\x7e]{{{MIN_KEY_LEN},}}")
LOCK_NAME = ".keys.lock"
NAME_RULE = "a-z, 0-9 and '-', 1-32 characters, not starting with '-'"

log = logging.getLogger("syncme.keys")


class KeyFileError(Exception):
    pass


def keys_path() -> Path:
    from .config import settings

    return settings.SYNCME_KEYS_FILE or (settings.DATA_DIR / "keys.toml")


def read_clients(path: Path) -> dict[str, dict[str, str]]:
    """Parse and validate the key file. Error messages never contain key values."""
    try:
        st = path.stat()
    except FileNotFoundError:
        raise KeyFileError(f"key file not found: {path}") from None
    except OSError as e:                         # permission denied, a parent that is not a directory, ...
        raise KeyFileError(f"key file {path} is not accessible: {e.strerror}") from None
    if st.st_mode & 0o077:
        raise KeyFileError(f"key file {path} must not be accessible by group or others (chmod 600)")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise KeyFileError(f"key file {path} is not valid TOML: {e}") from None
    except UnicodeDecodeError:                   # deliberately without the error text: it quotes file bytes
        raise KeyFileError(f"key file {path} is not valid UTF-8") from None
    except OSError as e:                         # a directory, a file that is not readable, ...
        raise KeyFileError(f"key file {path} is not readable: {e.strerror}") from None

    clients = data.get("clients")
    if not isinstance(clients, dict) or not clients:
        raise KeyFileError(f"key file {path} defines no clients")

    out: dict[str, dict[str, str]] = {}
    for name, entry in clients.items():
        if not NAME_RE.fullmatch(name):
            raise KeyFileError(f"invalid client name {name!r} (allowed: {NAME_RULE})")
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise KeyFileError(
                f"client {name!r}: key is missing or shorter than {MIN_KEY_LEN} characters, "
                "or contains whitespace or non-ASCII characters"
            )
        created = entry.get("created", "")
        out[name] = {"key": key, "created": str(created)}
    if len({e["key"] for e in out.values()}) != len(out):
        raise KeyFileError("two clients share the same key")
    return out


def _stamp(path: Path):
    st = path.stat()
    return (st.st_mtime_ns, st.st_ino, st.st_size)


class KeyStore:
    def __init__(self) -> None:
        self._keys: dict[str, str] = {}
        self._path: Path | None = None
        self._stamp = None
        # Requests are served from several threads. Without this lock a slow reload could publish an
        # older key set after a faster one had already published a newer one.
        self._lock = threading.Lock()

    def load(self, path: Path | None = None) -> None:
        from .config import settings

        if settings.SYNCME_API_KEY is not None:
            raise KeyFileError(
                "SYNCME_API_KEY is no longer supported: create per-client keys with "
                "`python -m app.keys add <name>` and remove the variable"
            )
        path = path or keys_path()
        with self._lock:
            try:
                stamp = _stamp(path)                 # stamp BEFORE reading: a change during the read is
            except OSError:                          # then seen by the next reload
                stamp = None                         # read_clients reports the problem as a KeyFileError
            clients = read_clients(path)
            self._keys = {n: e["key"] for n, e in clients.items()}
            self._path = path
            self._stamp = stamp

    def set_keys(self, keys: dict[str, str]) -> None:
        """Test helper: install keys directly, without a file."""
        with self._lock:
            self._keys = dict(keys)
            self._path = None
            self._stamp = None

    def _reload_if_changed(self) -> None:
        with self._lock:
            if self._path is None:
                return
            try:
                stamp = _stamp(self._path)
            except OSError:
                if self._stamp is not None:
                    log.error("ignoring invalid key file: %s is not readable", self._path)
                    self._stamp = None
                return
            if stamp == self._stamp:
                return
            self._stamp = stamp                  # log a bad file once, not on every request
            try:
                clients = read_clients(self._path)
            except KeyFileError as e:
                log.error("ignoring invalid key file: %s", e)
                return
            self._keys = {n: e["key"] for n, e in clients.items()}

    def authenticate(self, token: str) -> str | None:
        self._reload_if_changed()
        candidate = token.encode("utf-8", "surrogatepass")
        match = None
        for name, key in self._keys.items():     # no early exit: constant work per entry
            if secrets.compare_digest(candidate, key.encode()):
                match = name
        return match


keystore = KeyStore()


def _toml_str(value: str) -> str:
    # JSON string escapes (quote, backslash, control characters) are valid TOML basic-string escapes.
    # DEL is not escaped by JSON but is not allowed raw in TOML.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _render(clients: dict[str, dict[str, str]]) -> str:
    # The file is machine-managed: comments are not preserved. Client names are validated against
    # NAME_RE on read and add, so they are safe as bare keys; values are escaped.
    lines: list[str] = []
    for name in sorted(clients):
        e = clients[name]
        lines += [f"[clients.{name}]", f"key = {_toml_str(e['key'])}", f"created = {_toml_str(e.get('created', ''))}", ""]
    return "\n".join(lines)


def _write(path: Path, clients: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keys-", suffix=".tmp")   # created 0600
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(_render(clients))
            f.flush()
            os.fsync(f.fileno())                 # the data must be on disk before the rename publishes it
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@contextlib.contextmanager
def _locked(path: Path, create_dir: bool):
    """Hold an exclusive lock for a whole read-modify-write of the key file.

    The lock is flock() on a file in the key directory, so it works between processes (for example two
    `docker compose run keys ...` calls). It is released when the descriptor is closed.
    """
    if create_dir:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.is_dir():
        yield                                    # nothing to protect; read_clients reports the missing file
        return
    lock = path.parent / LOCK_NAME
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o600)
    except OSError as e:
        raise KeyFileError(
            f"cannot open lock file {lock}: {e.strerror}. The key directory must be writable by uid "
            f"{os.getuid()} (docker-compose runs the keys service as KEYS_UID:KEYS_GID, default 1000)"
        ) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _load_or_empty(path: Path) -> dict[str, dict[str, str]]:
    return read_clients(path) if path.exists() else {}


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _cmd_add(name: str) -> int:
    if not NAME_RE.fullmatch(name):
        return _err(f"invalid client name {name!r} (allowed: {NAME_RULE})")
    path = keys_path()
    with _locked(path, create_dir=True):
        clients = _load_or_empty(path)
        if name in clients:
            return _err(f"client {name!r} already exists")
        key = secrets.token_urlsafe(32)
        clients[name] = {"key": key, "created": datetime.date.today().isoformat()}
        _write(path, clients)
    print(key)                                   # stdout only, once; never logged
    return 0


def _cmd_list() -> int:
    for name, e in sorted(read_clients(keys_path()).items()):
        print(f"{name}\t{e.get('created', '')}")
    return 0


def _cmd_revoke(name: str) -> int:
    path = keys_path()
    with _locked(path, create_dir=False):
        clients = read_clients(path)
        if name not in clients:
            return _err(f"client {name!r} not found")
        if len(clients) == 1:
            return _err(
                "refusing to revoke the last client: an empty key file is invalid, so a running "
                "server would keep accepting the revoked key until restart. Add a new key first."
            )
        del clients[name]
        _write(path, clients)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.keys", description="Manage SyncMe client keys")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add", help="create a key for a new client").add_argument("name")
    sub.add_parser("list", help="list client names and creation dates")
    sub.add_parser("revoke", help="remove a client's key").add_argument("name")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "add":
            return _cmd_add(args.name)
        if args.cmd == "list":
            return _cmd_list()
        return _cmd_revoke(args.name)
    except KeyFileError as e:
        return _err(str(e))


if __name__ == "__main__":
    sys.exit(main())
