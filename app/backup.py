"""Consistent backups of the server state while the server runs, and restore from them.

Archive layout (a gzipped tar, regular files only):

    manifest.json
    db/server_state.db                  SQLite snapshot taken with the online backup API
    storage/objects/<ws>/<aa>/<rest>    CAS objects (file contents of every live file and kept version)
    storage/workspaces/<ws>/<path>      live files
    storage/archive/<ws>/<name>         archived (deleted) files
    keys/keys.toml                      only with include_keys: it holds the client tokens in plain text

The database is snapshotted first. CAS objects are immutable and written before the row that references
them is committed, so every object the snapshot references exists, unless a prune removes it meanwhile:
that is detected and the backup fails instead of writing an archive that cannot be restored. Workspace
files are copied later than the snapshot and may disagree with it; restore rewrites those from CAS.
"""
import datetime
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import zlib
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from . import db, keys, storage
from .validation import RESERVED_PREFIX, normalize_path, validate_workspace_id

log = logging.getLogger("syncme.backup")

FORMAT = "syncme-backup"
VERSION = 1
NAME_RE = re.compile(r"syncme-backup-\d{8}T\d{6}Z\.tar\.gz")
STAMP = "%Y%m%dT%H%M%SZ"
# Restoring without a live revision counter cannot know how far clients got past the backup; jumping this far
# ahead keeps the server from issuing a revision number a client has already seen for different content.
REVISION_GAP = 1_000_000

MANIFEST = "manifest.json"
DB_MEMBER = "db/server_state.db"
KEYS_MEMBER = "keys/keys.toml"
STORAGE_PARTS = ("objects", "workspaces", "archive")
_HEX = re.compile(r"[0-9a-f]+")


class BackupError(Exception):
    pass


def _utc(now: datetime.datetime | None) -> datetime.datetime:
    return (now or datetime.datetime.now(datetime.UTC)).astimezone(datetime.UTC)


def _storage_targets() -> dict[str, Path]:
    return {
        "objects": storage.get_cas_base_dir(),
        "workspaces": storage.get_files_dir() / "workspaces",
        "archive": storage.get_base_archive_dir(),
    }


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_db(path: Path) -> tuple[set[tuple[str, str]], dict[str, int]]:
    """CAS objects referenced by a database file, and its revision counter per workspace."""
    conn = sqlite3.connect(path)
    try:
        referenced = {(r[0], r[1]) for r in conn.execute(db.REFERENCED_CAS_SQL)}
        revisions = {}
        for key, value in conn.execute("SELECT key, value FROM metadata WHERE key LIKE 'current_revision:%'"):
            try:
                revisions[key.split(":", 1)[1]] = int(value)
            except (TypeError, ValueError):
                raise BackupError(f"the database has a non-numeric revision counter for {key!r}") from None
    finally:
        conn.close()
    return referenced, revisions


def _snapshot_db(src_path: Path, out: Path) -> None:
    os.close(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _add_file(tar: tarfile.TarFile, path: Path, arcname: str) -> bool:
    """Add one regular file, read through one open fd. Skip (with a warning) what verify would reject."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:          # deleted or moved to the archive while the backup ran
        return False
    except OSError as e:               # a symlink swapped in (ELOOP), not readable, ...
        log.warning("skipping %r: %s", str(path), e.strerror)
        return False
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False
        info = tar.gettarinfo(arcname=arcname, fileobj=f)
        info.type, info.linkname, info.size = tarfile.REGTYPE, "", st.st_size   # a hardlink is stored as a copy
        try:
            _check_member(info)
        except BackupError as e:
            log.warning("skipping %r: %s", str(path), e)
            return False
        tar.addfile(info, f)
    return True


def _add_objects(tar: tarfile.TarFile, base: Path) -> set[tuple[str, str]]:
    present = set()
    for ws_dir in sorted(base.iterdir()):
        if ws_dir.is_symlink() or not ws_dir.is_dir():
            continue
        for prefix_dir in sorted(ws_dir.iterdir()):
            if prefix_dir.is_symlink() or not prefix_dir.is_dir():
                continue
            for obj in sorted(prefix_dir.iterdir()):
                if obj.name.startswith("cas.") or obj.is_symlink() or not obj.is_file():   # "cas." = temp file
                    continue
                if _add_file(tar, obj, f"storage/objects/{ws_dir.name}/{prefix_dir.name}/{obj.name}"):
                    present.add((ws_dir.name, prefix_dir.name + obj.name))
    return present


def _add_tree(tar: tarfile.TarFile, base: Path, prefix: str, skip: Path | None = None) -> int:
    count = 0
    if not base.is_dir():
        return 0
    for root, dirs, files in os.walk(base):          # does not follow symlinked directories
        dirs.sort()
        for name in sorted(files):
            p = Path(root) / name
            if name.startswith(RESERVED_PREFIX) or p.is_symlink() or not p.is_file():
                continue
            if skip is not None and p.is_relative_to(skip):
                continue
            if _add_file(tar, p, f"{prefix}/{p.relative_to(base).as_posix()}"):
                count += 1
    return count


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mtime: float) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = mtime
    tar.addfile(info, io.BytesIO(data))


def apply_retention(dest: Path, keep: int, protect: Path) -> list[Path]:
    """Delete all but the newest `keep` backups in `dest`, never `protect`. Only names this module writes."""
    if keep <= 0:
        return []
    others = sorted(p for p in dest.iterdir() if NAME_RE.fullmatch(p.name) and p.name != protect.name and p.is_file())
    doomed = others[: max(0, len(others) - (keep - 1))]
    for p in doomed:
        p.unlink()
    return doomed


def create_backup(dest: Path, *, include_keys: bool = False, keep: int = 0,
                  now: datetime.datetime | None = None) -> Path:
    if keep < 0:
        raise ValueError("keep must be >= 0")
    dest = Path(dest)
    if not dest.is_dir():
        raise BackupError(f"backup destination {dest} is not a directory")
    db_path = db.get_db_path()
    if not db_path.is_file():          # checked first: opening it would create an empty database
        raise BackupError(f"no server database at {db_path}; nothing to back up")
    keys_file = keys.keys_path()
    if include_keys and not keys_file.is_file():
        raise BackupError(f"key file {keys_file} not found")

    now = _utc(now)
    final = dest / f"syncme-backup-{now.strftime(STAMP)}.tar.gz"
    if os.path.lexists(final):
        raise BackupError(f"{final} already exists; refusing to overwrite it")
    partial = dest / f".{final.name}.partial"
    snapshot = dest / f".{final.name}.db"
    targets = _storage_targets()
    try:
        partial.unlink(missing_ok=True)   # leftovers of a crashed run with the same name
        snapshot.unlink(missing_ok=True)
        _snapshot_db(db_path, snapshot)
        referenced, revisions = _read_db(snapshot)
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            with tarfile.open(fileobj=f, mode="w:gz") as tar:
                tar.add(snapshot, arcname=DB_MEMBER)
                present = _add_objects(tar, targets["objects"])
                missing = referenced - present
                if missing:
                    raise BackupError(
                        f"{len(missing)} CAS object(s) referenced by the database are missing "
                        "(was a prune running at the same time?); retry the backup"
                    )
                counts = {
                    "objects": len(present),
                    "workspace_files": _add_tree(tar, targets["workspaces"], "storage/workspaces",
                                                 skip=targets["archive"]),
                    "archive_files": _add_tree(tar, targets["archive"], "storage/archive"),
                }
                if include_keys:
                    tar.add(keys_file, arcname=KEYS_MEMBER)
                manifest = {
                    "format": FORMAT,
                    "version": VERSION,
                    "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "include_keys": include_keys,
                    "workspaces": revisions,
                    "counts": counts,
                }
                _add_bytes(tar, MANIFEST, json.dumps(manifest, indent=2).encode(), now.timestamp())
            f.flush()
            os.fsync(f.fileno())
        verify_backup(partial)          # old backups are deleted below: prove this one is readable first
        if os.path.lexists(final):
            raise BackupError(f"{final} already exists; refusing to overwrite it")
        os.rename(partial, final)
        _fsync_dir(dest)
    finally:
        snapshot.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
    apply_retention(dest, keep, protect=final)
    return final


def _check_member(info: tarfile.TarInfo) -> tuple[str, str]:
    """Return (component, relative path) for a member this module could have written; raise otherwise."""
    name = info.name
    if info.issym() or info.islnk():
        raise BackupError(f"archive contains a link: {name!r}")
    if not info.isfile():
        raise BackupError(f"archive contains an unexpected non-file member: {name!r}")
    parts = PurePosixPath(name).parts
    if name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in parts) \
            or "/".join(parts) != name:
        raise BackupError(f"archive member has an unsafe path: {name!r}")
    if name in (MANIFEST, DB_MEMBER, KEYS_MEMBER):
        return name, ""
    if len(parts) >= 4 and parts[0] == "storage" and parts[1] in STORAGE_PARTS:
        try:
            validate_workspace_id(parts[2])
        except HTTPException:
            raise BackupError(f"archive member has an invalid workspace id: {name!r}") from None
        if parts[1] == "objects" and not (
            len(parts) == 5 and len(parts[3]) == 2 and len(parts[4]) == 62
            and _HEX.fullmatch(parts[3]) and _HEX.fullmatch(parts[4])
        ):
            raise BackupError(f"archive member is not a CAS object path: {name!r}")
        return parts[1], "/".join(parts[2:])
    raise BackupError(f"archive member outside the backup layout: {name!r}")


def _check_manifest(manifest: object, db_revisions: dict[str, int]) -> dict:
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("not a SyncMe backup: manifest.json is missing or foreign")
    if manifest.get("version") != VERSION:
        raise BackupError(f"unsupported backup format version {manifest.get('version')!r}")
    if not isinstance(manifest.get("created_at"), str) or not isinstance(manifest.get("include_keys"), bool):
        raise BackupError("manifest.json lacks created_at or include_keys")
    workspaces = manifest.get("workspaces")
    if not isinstance(workspaces, dict) or workspaces != db_revisions:
        raise BackupError("manifest.json workspaces do not match the revision counters in the database")
    for ws, rev in workspaces.items():
        try:
            validate_workspace_id(ws)
        except HTTPException:
            raise BackupError(f"manifest.json names an invalid workspace id: {ws!r}") from None
        if type(rev) is not int or rev < 0:
            raise BackupError(f"manifest.json has an invalid revision for {ws!r}")
    return manifest


def _verify(tar: tarfile.TarFile) -> dict:
    manifest = None
    present: set[tuple[str, str]] = set()
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="syncme-verify-") as tmp:
        db_copy = Path(tmp) / "server_state.db"
        for info in tar:
            component, rel = _check_member(info)
            if info.name in seen:
                raise BackupError(f"archive contains {info.name!r} twice")
            seen.add(info.name)
            src = tar.extractfile(info)
            if component == MANIFEST:
                try:
                    manifest = json.loads(src.read())
                except ValueError:
                    raise BackupError("manifest.json is not valid JSON") from None
            elif component == DB_MEMBER:
                with open(db_copy, "wb") as out:
                    shutil.copyfileobj(src, out)
            elif component == "objects":
                h = hashlib.sha256()
                while chunk := src.read(1 << 16):
                    h.update(chunk)
                ws, prefix, rest = rel.split("/")
                if h.hexdigest() != prefix + rest:
                    raise BackupError(f"checksum mismatch for {info.name!r}")
                present.add((ws, prefix + rest))
        if manifest is None:
            raise BackupError("not a SyncMe backup: manifest.json is missing")
        if DB_MEMBER not in seen:
            raise BackupError("the backup contains no database")
        try:
            referenced, revisions = _read_db(db_copy)
        except sqlite3.DatabaseError as e:
            raise BackupError(f"the database in the backup is not readable: {e}") from None
    _check_manifest(manifest, revisions)
    if manifest["include_keys"] != (KEYS_MEMBER in seen):
        raise BackupError("the manifest and the archive disagree about the keys file")
    missing = referenced - present
    if missing:
        raise BackupError(f"{len(missing)} CAS object(s) referenced by the database are missing from the backup")
    return manifest


def _verify_open(f) -> dict:
    try:
        with tarfile.open(fileobj=f, mode="r:gz") as tar:
            return _verify(tar)
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise BackupError(f"not a readable backup archive: {e}") from None


def verify_backup(archive: Path) -> dict:
    """Check an archive end to end (layout, checksums, referenced objects, manifest) and return its manifest."""
    try:
        f = open(archive, "rb")
    except OSError as e:
        raise BackupError(f"{archive} cannot be opened: {e.strerror}") from None
    with f:
        return _verify_open(f)


def _occupied(path: Path) -> bool:
    if path.is_dir() and not path.is_symlink():
        return any(path.iterdir())
    return os.path.lexists(path)


def _live_revisions(db_path: Path) -> dict[str, int]:
    """Revision counters of the database being replaced; empty when it is missing or damaged."""
    if not db_path.is_file():
        return {}
    try:
        return _read_db(db_path)[1]
    except (sqlite3.DatabaseError, BackupError):   # a damaged database is a reason to restore, not to stop
        return {}


def _seed_revisions(db_file: Path, backup_revs: dict[str, int], live: dict[str, int]) -> None:
    # A workspace without a live counter (no live database, an empty or damaged one) gets the gap.
    seeded = {ws: (max(rev, live[ws]) if ws in live else rev + REVISION_GAP) for ws, rev in backup_revs.items()}
    seeded.update({ws: rev for ws, rev in live.items() if ws not in backup_revs})
    conn = sqlite3.connect(db_file)
    try:
        with conn:
            for ws, rev in seeded.items():
                conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                             (f"current_revision:{ws}", str(rev)))
    finally:
        conn.close()


def _repair_workspaces(db_file: Path, workspaces: Path, objects: Path) -> int:
    """Make every live row's file match the snapshot: rewrite it from its CAS object if missing or different."""
    conn = sqlite3.connect(db_file)
    try:
        rows = conn.execute("SELECT workspace_id, path, sha256, size, mtime FROM files WHERE is_deleted = 0").fetchall()
    finally:
        conn.close()
    repaired = 0
    for ws, path, sha, size, mtime in rows:
        try:
            validate_workspace_id(ws)
            ok = normalize_path(path) == path and isinstance(sha, str) and len(sha) == 64 and bool(_HEX.fullmatch(sha))
        except (HTTPException, TypeError):
            ok = False
        target = workspaces / ws / path
        if not ok or not target.resolve().is_relative_to((workspaces / ws).resolve()):
            log.warning("skipping database row with an unsafe path or hash: %r/%r", ws, path)
            continue
        try:
            st = target.lstat()
            if stat.S_ISREG(st.st_mode) and st.st_size == size and abs(st.st_mtime - mtime) < 1e-3:
                continue                   # the same test startup indexing uses to call a file unchanged
        except FileNotFoundError:
            pass
        src = objects / ws / sha[:2] / sha[2:]
        if target.is_dir() and not target.is_symlink():
            log.warning("cannot restore %r/%r: a directory is in the way", ws, path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(RESERVED_PREFIX + "restore-" + target.name[:32])
        shutil.copyfile(src, tmp)
        os.utime(tmp, (mtime, mtime))
        os.replace(tmp, target)
        repaired += 1
    return repaired


def restore_backup(archive: Path, *, force: bool = False, include_keys: bool = False,
                   now: datetime.datetime | None = None) -> dict:
    """Replace the server state with a backup. The server must be stopped. Existing data is moved aside."""
    archive = Path(archive).resolve()
    db.close_conn()
    try:
        lock_fd = db.lock_server_state(exclusive=True)    # also keeps the server from starting meanwhile
    except BlockingIOError:
        raise BackupError("the server is running (it holds the state lock); stop it before restoring") from None
    try:
        return _restore_locked(archive, force=force, include_keys=include_keys, stamp=_utc(now).strftime(STAMP))
    finally:
        os.close(lock_fd)
        db.close_conn()


def _restore_locked(archive: Path, *, force: bool, include_keys: bool, stamp: str) -> dict:
    try:
        fh = open(archive, "rb")             # verify and extract the same open file, not the path twice
    except OSError as e:
        raise BackupError(f"{archive} cannot be opened: {e.strerror}") from None
    with fh:
        manifest = _verify_open(fh)
        if include_keys and not manifest["include_keys"]:
            raise BackupError("the backup contains no keys file (it was made without --include-keys)")

        db_path = db.get_db_path()
        targets: dict[str, Path] = {DB_MEMBER: db_path, **_storage_targets()}
        if include_keys:
            targets[KEYS_MEMBER] = keys.keys_path()
        for a in targets.values():
            for b in targets.values():
                if a != b and b.is_relative_to(a):
                    raise BackupError(f"{b} is inside {a}; restore does not support nested storage directories")
        occupied = [str(p) for p in targets.values() if _occupied(p)]
        if occupied and not force:
            raise BackupError(f"existing data at {', '.join(occupied)}; pass --force to move it aside and restore")

        live = _live_revisions(db_path)
        staged = {c: t.parent / f".restore-{stamp}" / t.name for c, t in targets.items()}
        stage_roots = {p.parent for p in staged.values()}
        aside_roots = {t.parent / f".pre-restore-{stamp}" for t in targets.values()}
        for root in aside_roots:
            if os.path.lexists(root):
                raise BackupError(f"{root} already exists (a restore in the same second?); retry in a moment")
        moved: list[str] = []
        swapping = False
        try:
            for root in stage_roots:
                root.mkdir(mode=0o700)
            fh.seek(0)
            with tarfile.open(fileobj=fh, mode="r:gz") as tar:
                for info in tar:
                    component, rel = _check_member(info)
                    if component not in targets:
                        continue
                    target = staged[component]
                    info.name = rel if rel else target.name
                    tar.extract(info, path=target if rel else target.parent, filter="data")
            for component in (DB_MEMBER, KEYS_MEMBER):
                if component in staged:
                    os.chmod(staged[component], 0o600)
            _seed_revisions(staged[DB_MEMBER], manifest["workspaces"], live)
            repaired = _repair_workspaces(staged[DB_MEMBER], staged["workspaces"], staged["objects"])

            swapping = True
            for component, target in targets.items():
                olds = [target]
                if component == DB_MEMBER:
                    olds += [target.with_name(target.name + s) for s in ("-wal", "-shm")]
                for old in olds:
                    if old.is_dir() and not old.is_symlink() and not any(old.iterdir()):
                        old.rmdir()
                    elif os.path.lexists(old):
                        aside = old.parent / f".pre-restore-{stamp}"
                        aside.mkdir(mode=0o700, exist_ok=True)   # ours: checked above that none existed
                        os.rename(old, aside / old.name)
                        moved.append(str(aside / old.name))
                if os.path.lexists(staged[component]):
                    os.rename(staged[component], target)
        except OSError as e:
            if not swapping:
                raise BackupError(f"restore failed before any live data was touched: {e}") from None
            raise BackupError(
                f"restore failed halfway: {e}. Already moved aside: {', '.join(moved) or 'nothing'}. "
                "Run the restore again with --force once the cause is fixed."
            ) from None
        finally:
            for root in stage_roots:
                shutil.rmtree(root, ignore_errors=True)

    return {
        "manifest": manifest,
        "moved_aside": moved,
        "repaired_files": repaired,
        "workspaces": {
            ws: {"backup": manifest["workspaces"].get(ws), "live": live.get(ws), "next": db.get_revision(ws) + 1}
            for ws in sorted(manifest["workspaces"].keys() | live.keys())
        },
    }
