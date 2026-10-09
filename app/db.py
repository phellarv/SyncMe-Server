import sqlite3
import time
from pathlib import Path
from fastapi import HTTPException, status
from .config import settings
from .validation import RESERVED_PREFIX, normalize_path, validate_workspace_id
import threading

# Use thread-local connections since sqlite doesn't like shared connections
_local = threading.local()

def get_db_path() -> Path:
    p = (settings.DB_PATH or (settings.DATA_DIR / "server_state.db")).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(get_db_path(), isolation_level=None) # autocommit mode
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL;")
        _local.conn.execute("PRAGMA busy_timeout=5000;")
        init_db(_local.conn)
    return _local.conn

def init_db(conn: sqlite3.Connection):
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                workspace_id TEXT,
                path TEXT,
                sha256 TEXT,
                size INTEGER,
                mtime REAL,
                revision INTEGER,
                is_deleted BOOLEAN,
                PRIMARY KEY (workspace_id, path)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_versions (
                workspace_id TEXT NOT NULL,
                path TEXT NOT NULL,
                revision INTEGER NOT NULL,
                sha256 TEXT,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                client_name TEXT,
                created_at REAL NOT NULL,
                is_deleted BOOLEAN NOT NULL DEFAULT 0,
                PRIMARY KEY (workspace_id, path, revision)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_file_versions_path 
            ON file_versions (workspace_id, path, revision DESC)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_state (
                client_name TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                last_seen REAL NOT NULL,
                last_synced_revision INTEGER NOT NULL DEFAULT 0,
                last_ip TEXT,
                PRIMARY KEY (client_name, workspace_id)
            )
        """)

def get_revision(workspace_id: str) -> int:
    conn = get_conn()
    cursor = conn.execute("SELECT value FROM metadata WHERE key = ?", (f'current_revision:{workspace_id}',))
    row = cursor.fetchone()
    if not row:
        return 0
    return int(row[0])

def _increment_revision(conn: sqlite3.Connection, workspace_id: str) -> int:
    key = f'current_revision:{workspace_id}'
    cursor = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,))
    row = cursor.fetchone()
    current = int(row[0]) if row else 0
    new_rev = current + 1
    if row:
        conn.execute("UPDATE metadata SET value = ? WHERE key = ?", (str(new_rev), key))
    else:
        conn.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", (key, str(new_rev)))
    return new_rev

def get_state(workspace_id: str, since_revision: int = 0) -> tuple[list[dict], int]:
    conn = get_conn()
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        cursor = conn.execute("SELECT * FROM files WHERE workspace_id = ? AND revision > ?", (workspace_id, since_revision))
        files = [dict(row) for row in cursor.fetchall()]
        latest_revision = get_revision(workspace_id)
        if files:
            max_rev = max(f["revision"] for f in files)
            if latest_revision < max_rev:
                latest_revision = max_rev
    return files, latest_revision

def get_file(workspace_id: str, path: str) -> dict | None:
    conn = get_conn()
    cursor = conn.execute("SELECT * FROM files WHERE workspace_id = ? AND path = ?", (workspace_id, path))
    row = cursor.fetchone()
    return dict(row) if row else None

def upsert_file(workspace_id: str, path: str, sha256: str, size: int, mtime: float, expected_revision: int | None = None, before_commit=None, client_name: str | None = None) -> int:
    conn = get_conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute("SELECT revision FROM files WHERE workspace_id = ? AND path = ?", (workspace_id, path))
            row = cursor.fetchone()
            current_revision = row[0] if row else None
            
            if expected_revision is not None:
                if current_revision is None and expected_revision != 0:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="File does not exist")
                if current_revision is not None and expected_revision != current_revision:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Revision mismatch")
            
            new_rev = _increment_revision(conn, workspace_id)
            
            conn.execute("""
                INSERT INTO files (workspace_id, path, sha256, size, mtime, revision, is_deleted)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(workspace_id, path) DO UPDATE SET
                    sha256 = excluded.sha256,
                    size = excluded.size,
                    mtime = excluded.mtime,
                    revision = excluded.revision,
                    is_deleted = 0
            """, (workspace_id, path, sha256, size, mtime, new_rev))
            
            conn.execute("""
                INSERT INTO file_versions (workspace_id, path, revision, sha256, size, mtime, client_name, created_at, is_deleted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (workspace_id, path, new_rev, sha256, size, mtime, client_name, time.time()))

            if before_commit is not None:
                # e.g. os.replace(tmp, target): if it raises, the except below rolls the row back,
                # so a state row never exists without its file.
                before_commit()
            return new_rev
        except Exception:
            conn.execute("ROLLBACK")
            raise

def delete_file(workspace_id: str, path: str, expected_revision: int | None = None, client_name: str | None = None) -> int:
    conn = get_conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute("SELECT revision, is_deleted FROM files WHERE workspace_id = ? AND path = ?", (workspace_id, path))
            row = cursor.fetchone()
            if not row or row[1]:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
            current_revision = row[0]
            
            if expected_revision is not None and expected_revision != current_revision:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Revision mismatch")
            
            from . import storage
            storage.archive_file(workspace_id, path)
            
            new_rev = _increment_revision(conn, workspace_id)
            
            conn.execute("""
                UPDATE files
                SET is_deleted = 1, revision = ?
                WHERE workspace_id = ? AND path = ?
            """, (new_rev, workspace_id, path))
            
            conn.execute("""
                INSERT INTO file_versions (workspace_id, path, revision, sha256, size, mtime, client_name, created_at, is_deleted)
                VALUES (?, ?, ?, NULL, 0, ?, ?, ?, 1)
            """, (workspace_id, path, new_rev, time.time(), client_name, time.time()))
            
            return new_rev
        except Exception:
            conn.execute("ROLLBACK")
            raise

def get_file_history(workspace_id: str, path: str, limit: int = 50) -> list[dict]:
    conn = get_conn()
    cursor = conn.execute("""
        SELECT * FROM file_versions 
        WHERE workspace_id = ? AND path = ? 
        ORDER BY revision DESC LIMIT ?
    """, (workspace_id, path, limit))
    return [dict(row) for row in cursor.fetchall()]

def get_file_version(workspace_id: str, path: str, revision: int) -> dict | None:
    conn = get_conn()
    cursor = conn.execute("""
        SELECT * FROM file_versions
        WHERE workspace_id = ? AND path = ? AND revision = ?
    """, (workspace_id, path, revision))
    row = cursor.fetchone()
    return dict(row) if row else None

def record_client_activity(client_name: str, workspace_id: str, revision: int = 0, ip: str = "") -> None:
    try:
        conn = get_conn()
        now = time.time()
        clean_ip = ip.strip() if isinstance(ip, str) else ""
        with conn:
            conn.execute("""
                INSERT INTO client_state (client_name, workspace_id, last_seen, last_synced_revision, last_ip)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(client_name, workspace_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    last_synced_revision = CASE
                        WHEN excluded.last_synced_revision > 0 THEN MAX(client_state.last_synced_revision, excluded.last_synced_revision)
                        ELSE client_state.last_synced_revision
                    END,
                    last_ip = CASE
                        WHEN excluded.last_ip IS NOT NULL AND excluded.last_ip != '' THEN excluded.last_ip
                        ELSE client_state.last_ip
                    END
            """, (client_name, workspace_id, now, max(0, revision), clean_ip if clean_ip else None))
    except Exception as e:
        import logging
        logging.getLogger("syncme.db").warning("record_client_activity failed: %s", e)

def get_clients_state() -> list[dict]:
    conn = get_conn()
    cursor = conn.execute("SELECT * FROM client_state ORDER BY client_name, workspace_id")
    return [dict(row) for row in cursor.fetchall()]

def get_client_state(client_name: str) -> list[dict]:
    conn = get_conn()
    cursor = conn.execute("SELECT * FROM client_state WHERE client_name = ? ORDER BY workspace_id", (client_name,))
    return [dict(row) for row in cursor.fetchall()]

def get_workspaces_summary() -> list[dict]:
    conn = get_conn()
    cursor = conn.execute("""
        SELECT DISTINCT workspace_id FROM (
            SELECT substr(key, 18) AS workspace_id FROM metadata WHERE key LIKE 'current_revision:%'
            UNION
            SELECT workspace_id FROM files
            UNION
            SELECT workspace_id FROM file_versions
            UNION
            SELECT workspace_id FROM client_state
        ) WHERE workspace_id IS NOT NULL AND workspace_id != ''
        ORDER BY workspace_id
    """)
    ws_ids = [row[0] for row in cursor.fetchall()]
    summaries = []
    for ws_id in ws_ids:
        cur_rev = get_revision(ws_id)
        
        f_cur = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(MAX(mtime), 0.0)
            FROM files
            WHERE workspace_id = ? AND is_deleted = 0
        """, (ws_id,))
        f_row = f_cur.fetchone()
        file_count = f_row[0]
        total_size = f_row[1]
        max_mtime = float(f_row[2])
        
        v_cur = conn.execute("""
            SELECT client_name, created_at, revision
            FROM file_versions
            WHERE workspace_id = ?
            ORDER BY revision DESC, created_at DESC
            LIMIT 1
        """, (ws_id,))
        v_row = v_cur.fetchone()
        last_client = v_row["client_name"] if v_row else None
        last_v_created = float(v_row["created_at"]) if v_row else 0.0
        
        if v_row and v_row["revision"] > cur_rev:
            cur_rev = v_row["revision"]
            
        last_modified = max(max_mtime, last_v_created)
        
        summaries.append({
            "workspace_id": ws_id,
            "current_revision": cur_rev,
            "file_count": file_count,
            "total_size": total_size,
            "last_modified": last_modified,
            "last_client": last_client,
        })
    return summaries

def get_client_recent_files(client_name: str, limit: int = 10) -> list[dict]:
    conn = get_conn()
    cursor = conn.execute("""
        SELECT workspace_id, path, revision, size, created_at, is_deleted
        FROM file_versions
        WHERE client_name = ?
        ORDER BY created_at DESC, revision DESC
        LIMIT ?
    """, (client_name, limit))
    return [dict(row) for row in cursor.fetchall()]


def _printable(name: str) -> str:
    """Make a file name safe to print: no encoding errors, no control characters."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in name)

def _warn_skip(kind: str, name: str, reason: str) -> None:
    print(f"WARNING: index_existing_files: skipping {kind} '{_printable(name)}': {reason}")

def index_existing_files(files_dir: Path) -> int:
    import hashlib
    import logging
    from . import storage
    
    log = logging.getLogger("syncme.index")
    archive_dir = storage.get_base_archive_dir()
    
    conn = get_conn()
    updated_count = 0
    scanned_count = 0
    
    workspaces_dir = files_dir / "workspaces"
    if not workspaces_dir.exists():
        return 0
        
    log.info("Starting indexing of existing files in %s", workspaces_dir)
        
    for workspace_path in workspaces_dir.iterdir():
        if not workspace_path.is_dir():
            continue
        workspace_id = workspace_path.name
        try:
            validate_workspace_id(workspace_id)
        except HTTPException:
            _warn_skip("workspace directory", workspace_id, "invalid workspace id")
            continue
        workspace_root = workspace_path.resolve()
        
        for p in workspace_path.rglob("*"):
            if not p.is_file():
                continue
            if p.name.startswith(RESERVED_PREFIX):
                continue
                
            try:
                if p.is_relative_to(archive_dir):
                    continue
            except ValueError:
                pass
                
            rel_path = p.relative_to(workspace_path).as_posix()
            entry = f"{workspace_id}/{rel_path}"

            try:
                normalized = normalize_path(rel_path)
            except HTTPException:
                normalized = None
            if normalized != rel_path:
                _warn_skip("file", entry, "path is not a valid normalized path")
                continue

            try:
                rel_path.encode("utf-8")
            except UnicodeEncodeError:
                _warn_skip("file", entry, "name cannot be encoded as UTF-8")
                continue

            if not p.resolve().is_relative_to(workspace_root):
                _warn_skip("file", entry, "resolves outside its workspace directory")
                continue

            st = p.stat()
            size = st.st_size
            mtime = st.st_mtime
            
            scanned_count += 1
            if scanned_count % 100 == 0:
                log.info("Scanned %d files so far...", scanned_count)
            
            with conn:
                cursor = conn.execute("SELECT sha256, is_deleted, revision, mtime, size FROM files WHERE workspace_id = ? AND path = ?", (workspace_id, rel_path))
                row = cursor.fetchone()
                
                if row is not None and not row['is_deleted'] and row['size'] == size and abs(row['mtime'] - mtime) < 1e-3:
                    sha256 = row['sha256']
                    v_cur = conn.execute("SELECT 1 FROM file_versions WHERE workspace_id = ? AND path = ? AND revision = ? LIMIT 1", (workspace_id, rel_path, row['revision']))
                    if not v_cur.fetchone():
                        storage.store_cas_object(workspace_id, p, sha256)
                        conn.execute("""
                            INSERT OR IGNORE INTO file_versions (workspace_id, path, revision, sha256, size, mtime, client_name, created_at, is_deleted)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """, (workspace_id, rel_path, row['revision'], sha256, size, mtime, "baseline", time.time()))
                    continue

            # File is new, deleted, or mtime/size changed: read and calculate hash
            sha256 = hashlib.sha256(p.read_bytes()).hexdigest()
            
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = conn.execute("SELECT sha256, is_deleted, revision FROM files WHERE workspace_id = ? AND path = ?", (workspace_id, rel_path))
                    row = cursor.fetchone()
                    
                    if row is not None and not row['is_deleted'] and row['sha256'] == sha256:
                        # Content is unchanged (e.g. touched mtime) - just update metadata without bumping revision
                        conn.execute("UPDATE files SET mtime = ?, size = ? WHERE workspace_id = ? AND path = ?", (mtime, size, workspace_id, rel_path))
                        continue
                        
                    new_rev = _increment_revision(conn, workspace_id)
                    storage.store_cas_object(workspace_id, p, sha256)
                    
                    conn.execute("""
                        INSERT INTO files (workspace_id, path, sha256, size, mtime, revision, is_deleted)
                        VALUES (?, ?, ?, ?, ?, ?, 0)
                        ON CONFLICT(workspace_id, path) DO UPDATE SET
                            sha256 = excluded.sha256,
                            size = excluded.size,
                            mtime = excluded.mtime,
                            revision = excluded.revision,
                            is_deleted = 0
                    """, (workspace_id, rel_path, sha256, size, mtime, new_rev))
                    
                    conn.execute("""
                        INSERT INTO file_versions (workspace_id, path, revision, sha256, size, mtime, client_name, created_at, is_deleted)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """, (workspace_id, rel_path, new_rev, sha256, size, mtime, "index", time.time()))
                    
                    updated_count += 1
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                    
    log.info("Indexing complete. Scanned %d files, updated %d.", scanned_count, updated_count)
    return updated_count
