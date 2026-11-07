import hashlib
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import HTTPException
from .config import settings
from . import db
from .validation import ARCHIVE_SUFFIX_BYTES, RESERVED_PREFIX, normalize_path, validate_workspace_id

log = logging.getLogger("syncme.storage")

# An upload temp file older than this is an orphan: its upload cannot still be in flight.
STALE_TEMP_SECONDS = 3600

def get_files_dir() -> Path:
    d = (settings.STORAGE_DIR or settings.FILES_DIR or (settings.DATA_DIR / "files")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_workspace_dir(workspace_id: str) -> Path:
    validate_workspace_id(workspace_id)
    d = get_files_dir() / "workspaces" / workspace_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_cas_base_dir() -> Path:
    d = get_files_dir() / "objects"
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_cas_dir(workspace_id: str) -> Path:
    validate_workspace_id(workspace_id)
    d = get_cas_base_dir() / workspace_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_cas_object_path(workspace_id: str, sha256: str) -> Path:
    d = get_cas_dir(workspace_id)
    prefix = sha256[:2]
    suffix = sha256[2:]
    return d / prefix / suffix

def store_cas_object(workspace_id: str, src_path: Path | str, sha256: str) -> Path:
    target_path = get_cas_object_path(workspace_id, sha256)
    target_dir = target_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    
    if not target_path.exists():
        # Atomically store the CAS object
        with tempfile.NamedTemporaryFile(prefix="cas.", dir=target_dir, delete=False) as tmp:
            tmp_target = tmp.name
            with open(src_path, "rb") as src:
                shutil.copyfileobj(src, tmp)
            os.fsync(tmp.fileno())
        try:
            os.replace(tmp_target, target_path)
            if hasattr(os, "O_DIRECTORY"):
                try:
                    fd = os.open(target_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    os.fsync(fd)
                    os.close(fd)
                except OSError:
                    pass
        except Exception:
            Path(tmp_target).unlink(missing_ok=True)
            raise
            
    return target_path

def get_cas_object(workspace_id: str, sha256: str) -> Path | None:
    target_path = get_cas_object_path(workspace_id, sha256)
    if target_path.exists():
        return target_path
    return None


def get_base_archive_dir() -> Path:
    d = (settings.ARCHIVE_DIR or (get_files_dir() / "archive")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_archive_dir(workspace_id: str) -> Path:
    validate_workspace_id(workspace_id)
    d = get_base_archive_dir() / workspace_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_file_path(workspace_id: str, path: str) -> Path:
    d = get_workspace_dir(workspace_id)
    root = d.resolve()
    safe_path = (d / path).resolve()
    if safe_path == root or not safe_path.is_relative_to(root):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid path traversal")
    return safe_path

def archive_file(workspace_id: str, path: str):
    file_path = get_file_path(workspace_id, path)
    if file_path.exists():
        timestamp = str(int(time.time()))
        archive_name = f"{path}_{timestamp}"
        archive_dir = get_archive_dir(workspace_id)
        archive_path = archive_dir / archive_name
        if not archive_path.resolve().is_relative_to(archive_dir.resolve()):
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Invalid path traversal")
        # Two deletes of the same path within one second get the same name; never overwrite an archive.
        n = 0
        while os.path.lexists(archive_path):
            n += 1
            archive_path = archive_dir / f"{archive_name}-{n}"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(file_path), str(archive_path))
        try:
            os.utime(archive_path, None)
        except OSError:
            pass

def _conflict(exc: Exception):
    from fastapi import HTTPException
    return HTTPException(status_code=409, detail="Path conflicts with an existing file or directory")

def _commit_restored_file(
    workspace_id: str,
    target_path: str,
    src_path: Path,
    sha256_hex: str | None = None,
    use_source_mtime: bool = False,
    expected_revision: int | None = None,
    client_name: str | None = None,
) -> int:
    target = get_file_path(workspace_id, target_path)
    # The temp file is in the workspace root (same filesystem as the target); the target's directories are
    # created only inside before_commit, so a rejected restore leaves no directory and no temp file behind.
    fd, tmp = tempfile.mkstemp(dir=get_workspace_dir(workspace_id), prefix=RESERVED_PREFIX)
    os.close(fd)

    def commit_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, target)

    try:
        shutil.copy2(str(src_path), tmp)
        h = hashlib.sha256() if sha256_hex is None else None
        with open(tmp, "r+b") as f:
            if h is not None:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            os.fsync(f.fileno())            # the data is on disk before the rename makes it the file
        st = os.stat(tmp)
        if sha256_hex is None:
            sha256_hex = h.hexdigest()
            store_cas_object(workspace_id, tmp, sha256_hex)
        mtime = st.st_mtime if use_source_mtime else time.time()
        # The revision is checked inside upsert_file BEFORE commit_file runs, so a 409 leaves
        # the target file untouched.
        return db.upsert_file(
            workspace_id, target_path, sha256_hex, st.st_size, mtime,
            expected_revision, before_commit=commit_file, client_name=client_name,
        )
    except (IsADirectoryError, NotADirectoryError, FileExistsError) as e:
        raise _conflict(e) from None
    finally:
        Path(tmp).unlink(missing_ok=True)


def restore_file(
    workspace_id: str,
    archive_filename: str,
    target_path: str,
    expected_revision: int | None = None,
    client_name: str | None = None,
) -> int:
    archive_filename = normalize_path(archive_filename, slack=ARCHIVE_SUFFIX_BYTES)   # an archive name carries a suffix
    target_path = normalize_path(target_path)
    d = get_archive_dir(workspace_id)
    archive_path = (d / archive_filename).resolve()
    if not archive_path.is_relative_to(d.resolve()):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid path traversal")
    if not archive_path.is_file():
        raise FileNotFoundError("Archive file not found")

    return _commit_restored_file(
        workspace_id,
        target_path,
        archive_path,
        sha256_hex=None,
        use_source_mtime=True,
        expected_revision=expected_revision,
        client_name=client_name,
    )


def restore_cas_file(
    workspace_id: str,
    path: str,
    restore_revision: int,
    expected_revision: int | None = None,
    client_name: str | None = None,
) -> int:
    path = normalize_path(path)
    version = db.get_file_version(workspace_id, path, restore_revision)
    if not version or version.get("is_deleted"):
        raise FileNotFoundError("Revision not found or is deleted")
        
    sha256 = version.get("sha256")
    if not sha256:
        raise FileNotFoundError("Revision missing hash")
        
    cas_path = get_cas_object(workspace_id, sha256)
    if not cas_path or not cas_path.exists():
        raise FileNotFoundError("CAS object missing")

    return _commit_restored_file(
        workspace_id,
        path,
        cas_path,
        sha256_hex=sha256,
        use_source_mtime=False,
        expected_revision=expected_revision,
        client_name=client_name,
    )


def list_archive(workspace_id: str) -> list[dict]:
    archive_dir = get_archive_dir(workspace_id)
    files = []
    if not archive_dir.exists():
        return files
        
    for p in archive_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(archive_dir)
            files.append({
                "archive_filename": str(rel),
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime
            })
    return files

def sweep_stale_temp_files(max_age: float = STALE_TEMP_SECONDS) -> int:
    """Delete upload temp files that a crashed or killed process left in a workspace; return how many.

    Only files older than `max_age` seconds go: a younger one may belong to an upload that is in flight in
    another process. Symlinked directories are not followed."""
    root = get_files_dir() / "workspaces"
    cutoff = time.time() - max_age
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(root):          # a missing root simply yields nothing
        for name in filenames:
            if not name.startswith(RESERVED_PREFIX):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.lstat(path).st_mtime < cutoff:       # lstat: a symlink is judged, and removed, as itself
                    os.unlink(path)
                    removed += 1
            except FileNotFoundError:
                continue                                          # removed by someone else meanwhile
    if removed:
        log.info("removed %d orphaned upload temp file(s)", removed)
    return removed


def prune_storage(keep_versions: int = 10, archive_days: float | None = None, keep_days: float | None = None) -> dict:
    """Prune file history by count (`keep_versions` per file) or, when `keep_days` is set, by age instead."""
    if keep_versions < 1:
        raise ValueError("keep_versions must be >= 1")
    if keep_days is not None and keep_days < 0:
        raise ValueError("keep_days must be >= 0")
    if archive_days is not None and archive_days < 0:
        raise ValueError("archive_days must be >= 0")

    cas_base = get_cas_base_dir()
    if not os.access(cas_base, os.W_OK):
        raise PermissionError(f"Storage directory '{cas_base}' is not writable")
    if archive_days is not None:
        archive_base = get_base_archive_dir()
        if not os.access(archive_base, os.W_OK):
            raise PermissionError(f"Archive directory '{archive_base}' is not writable")

    conn = db.get_conn()
    db_has_data = (
        conn.execute("SELECT 1 FROM files LIMIT 1").fetchone() is not None
        or conn.execute("SELECT 1 FROM file_versions LIMIT 1").fetchone() is not None
    )

    if keep_days is not None:
        versions_deleted = db.prune_versions_older_than(time.time() - keep_days * 86400.0)
    else:
        versions_deleted = db.prune_old_versions(keep_versions=keep_versions)

    cas_deleted = 0
    if db_has_data and cas_base.exists():
        candidates: list[tuple[str, str, Path]] = []
        prefix_dirs: list[Path] = []
        for ws_dir in cas_base.iterdir():
            if not ws_dir.is_dir():
                continue
            ws_id = ws_dir.name
            for prefix_dir in ws_dir.iterdir():
                if not prefix_dir.is_dir():
                    continue
                prefix_dirs.append(prefix_dir)
                prefix = prefix_dir.name
                for obj_file in prefix_dir.iterdir():
                    if not obj_file.is_file() or obj_file.name.startswith("cas."):
                        continue
                    sha256 = prefix + obj_file.name
                    candidates.append((ws_id, sha256, obj_file))

        referenced = db.get_referenced_cas_hashes()
        for ws_id, sha256, obj_file in candidates:
            if (ws_id, sha256) not in referenced:
                try:
                    obj_file.unlink()
                    cas_deleted += 1
                except FileNotFoundError:
                    pass
        for prefix_dir in prefix_dirs:
            try:
                if not any(prefix_dir.iterdir()):
                    prefix_dir.rmdir()
            except OSError:
                pass

    archive_deleted = 0
    if archive_days is not None:
        cutoff = time.time() - (archive_days * 86400.0)
        archive_base = get_base_archive_dir()
        if archive_base.exists():
            for p in archive_base.rglob("*"):
                if p.is_file():
                    try:
                        if p.stat().st_mtime < cutoff:
                            p.unlink()
                            archive_deleted += 1
                    except FileNotFoundError:
                        pass

    return {
        "versions_deleted": versions_deleted,
        "cas_deleted": cas_deleted,
        "archive_deleted": archive_deleted,
    }


