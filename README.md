# SyncMe-Server

SyncMe-Server is the central coordination and storage service for the SyncMe ecosystem. Powered by FastAPI and asynchronous Python 3.12, it provides multi-directory workspace support, revision-indexed file storage, workspace-scoped optimistic concurrency control, real-time Server-Sent Events (SSE) notification streaming, and automated deletion archiving.

---

## Table of Contents
- [Architecture & Core Mechanics](#architecture--core-mechanics)
  - [Workspace Partitioning & Isolation](#workspace-partitioning--isolation)
  - [Revision Tracking & SQLite Schema](#revision-tracking--sqlite-schema)
  - [Server-Sent Events (SSE) Broadcaster](#server-sent-events-sse-broadcaster)
  - [Storage Layer & Path Traversal Defense](#storage-layer--path-traversal-defense)
  - [Automatic File Archiving](#automatic-file-archiving)
  - [Startup State Indexing & Cleanup](#startup-state-indexing--cleanup)
  - [Authentication](#authentication)
  - [Client Keys](#client-keys)
  - [Server Status & Telemetry CLI](#server-status--telemetry-cli)
- [Configuration](#configuration)
  - [Environment Variables](#environment-variables)
  - [Storage and Database Layout](#storage-and-database-layout)
- [Installation & Running](#installation--running)
  - [Local Execution](#local-execution)
  - [Docker & Docker Compose](#docker--docker-compose)
  - [Running as a non-root user](#running-as-a-non-root-user)
  - [Limits](#limits)
  - [Reverse proxy (HAProxy)](#reverse-proxy-haproxy)
- [API Reference](#api-reference)
- [Development & Testing](#development--testing)

---

## Architecture & Core Mechanics

### Workspace Partitioning & Isolation
SyncMe-Server organizes all data and interactions around **workspaces** identified by a `workspace_id`. This allows clients to synchronize multiple separate folders (e.g. `default`, `work`, `notes`) independently:
- **Isolated Storage:** Files for each workspace are kept in dedicated subdirectories on disk (`<STORAGE_DIR>/workspaces/{workspace_id}/`).
- **Isolated Revisions:** Revision counters and file states are tracked independently per workspace in SQLite.
- **Isolated SSE Streams:** Event subscriptions are scoped per workspace, ensuring clients only receive change notifications for the workspaces they actively monitor.
- **Isolated Archives:** Deleted files are preserved in workspace-specific archive directories (`<ARCHIVE_DIR>/{workspace_id}/`).

### Revision Tracking & SQLite Schema
SyncMe-Server maintains monotonically increasing revision counters per workspace (`current_revision:{workspace_id}` stored in the SQLite `metadata` table). Every file modification, restoration, or deletion increments the workspace's revision.

#### SQLite Schema
The server database (`server_state.db`) uses the following schema:

```sql
CREATE TABLE IF NOT EXISTS files (
    workspace_id TEXT,
    path TEXT,
    sha256 TEXT,
    size INTEGER,
    mtime REAL,
    revision INTEGER,
    is_deleted BOOLEAN,
    PRIMARY KEY (workspace_id, path)
);

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
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

- Each file record in `files` represents the latest HEAD state of `(workspace_id, path)`.
- `file_versions` tracks every mutation, upload, restore, and deletion (tombstone) across time.
- File contents are deduplicated and preserved in Content-Addressable Storage (`objects/{workspace_id}/{ab}/{cdef...}`) indexed by SHA-256.

- Each file record is uniquely identified by `(workspace_id, path)`.
- Metadata table stores keys such as `current_revision:<workspace_id>`.
- File reads (`GET /api/v1/workspaces/{workspace_id}/files/{path}`) provide the current file revision in an HTTP `ETag` header.
- File updates (`PUT /api/v1/workspaces/{workspace_id}/files/{path}`) and deletions (`DELETE /api/v1/workspaces/{workspace_id}/files/{path}`) accept an optional `If-Match: <expected_revision>` header:
  - If `expected_revision` matches the file's current revision, the operation increments `current_revision:{workspace_id}` and updates the record.
  - If `expected_revision == 0`, the server asserts that the file does not currently exist in the workspace.
  - If `expected_revision` differs from the database state, the server returns HTTP `409 Conflict`.
- Database operations run against SQLite with Write-Ahead Logging (`PRAGMA journal_mode=WAL;`) and a busy timeout (`PRAGMA busy_timeout=5000;`) to support high-concurrency access without lock contention.

### Server-Sent Events (SSE) Broadcaster
The server streams live workspace updates to connected clients via `GET /api/v1/workspaces/{workspace_id}/events`.
- Broadcaster manages active client async queues partitioned by `workspace_id`.
- Whenever a file is created, modified, or restored in a workspace, a `file_updated` event is broadcast to clients listening to that workspace.
- Whenever a file is removed in a workspace, a `file_deleted` event is broadcast.
- **Origin Client Suppression:** The event payload carries `origin_client_id`, the **name of the client whose key authenticated the mutation** (see [Client Keys](#client-keys)), so the originating client can filter out echo events. The `X-Client-ID` request header and the `client_id` field of the restore request are ignored by the server.

SSE payload structure:
```json
{
  "event": "file_updated",
  "path": "documents/report.pdf",
  "revision": 42,
  "origin_client_id": "laptop"
}
```

### Storage Layer & Path Traversal Defense
Files are stored under the directory specified by `STORAGE_DIR` (or `FILES_DIR`, falling back to `DATA_DIR / "files"`).
- Files for a workspace are located at `<STORAGE_DIR>/workspaces/{workspace_id}/{path}`.
- All paths submitted via endpoints are resolved using Python's `Path.resolve()`.
- The resolved path is verified using `safe_path.is_relative_to(workspace_dir)`. Any attempted directory traversal (e.g. `../../etc/passwd` or traversing out of the workspace sandbox) immediately raises `HTTPException(400, "Invalid path traversal")`.
- File uploads are written to a temporary file in the workspace's root directory (the target's directories are only created when the upload is committed, so a rejected upload leaves nothing behind), flushed to disk with `fsync` and then atomically moved into place (`os.replace`) to protect against partial writes or corrupted files during concurrent access.

### Automatic File Archiving
When a file is deleted via `DELETE /api/v1/workspaces/{workspace_id}/files/{path}`:
1. The active file on disk is moved into the workspace archive `<ARCHIVE_DIR>/{workspace_id}/{path}_{timestamp}` (`-1`, `-2`, ... is appended when two deletes of the same path fall in the same second, so no archive is overwritten).
2. The database record is marked with `is_deleted = 1` and assigned a new workspace revision.
3. Archived files can be inspected via `GET /api/v1/workspaces/{workspace_id}/archive` and restored using `POST /api/v1/workspaces/{workspace_id}/archive/restore`.

### Startup State Indexing & Cleanup
On application startup (`lifespan` handler):
- **Orphaned Upload Sweeping:** Upload temp files (`.syncme-upload-*`) older than one hour (`STALE_TEMP_SECONDS = 3600`), left behind by an interrupted or crashed upload, are safely removed from every workspace directory. Younger temp files are preserved because an upload may still be in progress.
- **Background State Indexing:** `db.index_existing_files()` runs asynchronously in a worker thread:
  - Scans all files on disk under `<STORAGE_DIR>/workspaces/`.
  - Populates missing CAS objects under `<STORAGE_DIR>/objects/{workspace_id}/` and seeds baseline `file_versions` records (`client_name = "baseline"`).
  - Any existing files not yet registered in SQLite (or whose SHA-256 hash or deletion flag does not match disk) are indexed into the database. If files were modified offline, new monotonic revisions are generated (`client_name = "index"`).

### Authentication
Endpoints (except `GET /health` and `GET /api/v1/health`) require `Authorization: Bearer <client key>`. Every client has its own key (see [Client Keys](#client-keys)).
- Only the `Authorization` header is accepted. The old `?token=` query parameter is not supported and gives `401`.
- Keys are compared with `secrets.compare_digest` against every entry, with no early exit, to prevent timing side-channel attacks.
- A failed attempt is logged with the source IP only, never with the presented key. The IP is the first entry of `X-Forwarded-For`, else the peer address. **The reverse proxy (e.g. HAProxy) must set `X-Forwarded-For`**; it is used only for logging failed logins and never for any access decision.
- The authenticated client name is used as `origin_client_id` in every broadcast event. The `X-Client-ID` header is ignored.

### Client Keys
Keys live in a TOML file, by default `<DATA_DIR>/keys.toml` (`SYNCME_KEYS_FILE`; Docker Compose uses `keys/keys.toml`):

```toml
[clients.laptop]
key = "<at least 32 printable ASCII characters, no whitespace>"
created = "2026-10-03"
```

- Client names match `[a-z0-9][a-z0-9-]{0,31}` (1-32 characters; a name must not start with a hyphen); keys are at least 32 printable ASCII characters (`[\x21-\x7e]`, no whitespace, so a key can always be sent in an HTTP header); no two clients may share a key.
- The file must not be accessible by group or others (`chmod 600`). A missing, empty, invalid or too loosely protected file stops the server at startup (fail closed). Error messages never contain key values.
- `SYNCME_API_KEY` has been removed. If it is still set (environment or `.env`), the server refuses to start and says so.
- Edits take effect **without a restart**: the file is re-read when its modification time, inode or size changes. If the new file is invalid, the last valid set of keys stays active and an error (without key values) is logged once.
- The key file is machine-managed; comments are not preserved when the CLI rewrites it.

Manage keys with the CLI. With Docker Compose (the `keys` service is in the opt-in `tools` profile):

```bash
install -d -o 1000 -g 1000 -m 700 keys          # first time only; must be writable by KEYS_UID/KEYS_GID (default 1000)
docker compose run --rm keys add laptop          # prints the new key once; copy it now, it is never logged
docker compose run --rm keys list                # names and creation dates, never keys
docker compose run --rm keys revoke phone
```

Without Docker: `python -m app.keys add|list|revoke`.

- `revoke` refuses to remove the last client: an empty file is invalid, so a running server would keep accepting the revoked key until restart. Add a new key first.
- Writes are atomic (temporary file in the same directory, then `os.replace`, mode `0600`). The **directory** is mounted into the container, not the single file, because a single-file bind mount cannot be replaced atomically. The `server` mounts it read-only.
- Set `KEYS_DIR` to use another host directory, and `KEYS_UID`/`KEYS_GID` if your user is not 1000:1000.

### Server Status & Telemetry CLI

The server provides built-in commands to monitor connected clients, synchronization lag across workspaces, and individual client activity.

With Docker Compose (the `cli` service is in the opt-in `tools` profile):

```bash
docker compose run --rm cli clients
docker compose run --rm cli status
docker compose run --rm cli client status <client_name>
```

Or execute directly inside a running `server` container:

```bash
docker compose exec server python -m app.cli clients
docker compose exec server python -m app.cli status
docker compose exec server python -m app.cli client status <client_name>
```

Without Docker:

```bash
python -m app.cli clients
python -m app.cli status
python -m app.cli client status <client_name>
```

- **`clients`**: Lists registered clients from `keys.toml` alongside active SSE connection state and last seen timestamps (`Online (SSE)`, `Idle`, `Offline`, `Never connected`), IP address, and active workspaces.
- **`status`**: Shows a per-workspace overview of the latest revision, active file count, total storage size, last modification timestamp, the client responsible for the latest change, and per-client synchronization lag.
- **`client status <name>`**: Shows details for a specific client, its sync lag per workspace, and its last 10 modified or deleted files.

---

## Configuration

Settings are loaded via `pydantic-settings` from environment variables or a `.env` file (searched in project root and current working directory):

### Essential `.env` Example

```dotenv
HOST=0.0.0.0
PORT=8000
DATA_DIR=./data
STORAGE_DIR=./storage
```

| Environment Variable | Type | Default Value | Description |
| :--- | :--- | :--- | :--- |
| `SYNCME_KEYS_FILE` | `path` | `None` (defaults to `<DATA_DIR>/keys.toml`) | TOML file with one key per client. See [Client Keys](#client-keys). |
| `SYNCME_API_KEY` | removed | not supported | The old shared key. The server refuses to start if it is still set. |
| `DATA_DIR` | `path` | `./data` | Base directory for database, archive, and fallback defaults. |
| `STORAGE_DIR` / `FILES_DIR` | `path` | `./storage` (in `.env.example`; defaults to `<DATA_DIR>/files`) | Base directory holding active workspace synchronized files (`workspaces/{workspace_id}`). |
| `ARCHIVE_DIR` | `path` | `None` (defaults to `<DATA_DIR>/archive`) | Base directory where deleted files are archived (`archive/{workspace_id}`). |
| `DB_PATH` | `path` | `None` (defaults to `<DATA_DIR>/server_state.db`) | Path to the SQLite state database file. |
| `HOST` | `string` | `"0.0.0.0"` | Network interface to bind the HTTP server. |
| `PORT` | `integer` | `8000` | Port to bind the HTTP server. |
| `MAX_UPLOAD_BYTES` | `integer` | `268435456` (256 MiB) | Largest accepted `PUT` body; larger uploads get `413`. See [Limits](#limits). |
| `SERVER_UID` / `SERVER_GID` | Compose only | `1000` / `1000` | User the `server` container runs as. See [Running as a non-root user](#running-as-a-non-root-user). |

### Storage and Database Layout

```
<DATA_DIR>/
├── server_state.db          # SQLite DB tracking files across all workspaces (WAL mode)
└── keys.toml                # Per-client API keys file (mode 0600)
<STORAGE_DIR>/
├── workspaces/
│   ├── default/             # Files synchronized in the 'default' workspace
│   │   └── <files>
│   └── work/                # Files synchronized in the 'work' workspace
│       └── <files>
├── objects/
│   ├── default/             # Content-Addressable Storage (CAS) for 'default' workspace
│   │   └── {sha256[:2]}/{sha256[2:]}
│   └── work/                # Content-Addressable Storage (CAS) for 'work' workspace
│       └── {sha256[:2]}/{sha256[2:]}
└── archive/
    ├── default/             # Archived deletions for 'default' workspace
    └── work/                # Archived deletions for 'work' workspace
```

---

## Installation & Running

### Local Execution
Requires Python >= 3.12.

1. **Clone and enter repository:**
   ```bash
   cd SyncMe-Server
   ```

2. **Create virtual environment and install dependencies:**
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env to set DATA_DIR, STORAGE_DIR, and custom settings
   python -m app.keys add <client-name>   # creates <DATA_DIR>/keys.toml and prints the key once
   ```

4. **Start the server:**
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

### Docker & Docker Compose
The repository includes a ready-to-run `Dockerfile` and `docker-compose.yml`.

1. **Configure environment:**
   ```bash
   cp .env.example .env
   ```

2. **Create the first client key** (the server refuses to start without one):
   ```bash
   install -d -o 1000 -g 1000 -m 700 keys   # a root-owned keys/ makes the keys service fail with "cannot open lock file"
   docker compose run --rm keys add <client-name>
   ```

3. **Start with Docker Compose:**
   ```bash
   docker compose up --build -d
   ```

To stop:
```bash
docker compose down
```

Mounted volumes in Compose:
- `${DATA_DIR:-./data}` -> `/data` (SQLite database and defaults)
- `${STORAGE_DIR:-./storage}` -> `/storage` (Active synchronized files)
- `${KEYS_DIR:-./keys}` -> `/keys` (Client key file `keys.toml`; read-only for the `server`, writable for the `keys` tool service)

Both services run with a read-only root filesystem (`/tmp` is a tmpfs), all Linux capabilities dropped and `no-new-privileges`. Only the mounted directories above are writable.

### Running as a non-root user
The image runs as uid/gid 1000 (user `app`), and `docker-compose.yml` starts the `server` as `${SERVER_UID:-1000}:${SERVER_GID:-1000}`.

- Before the first start, give that user the data and storage directories: `chown -R <uid>:<gid> <data dir> <storage dir>`. Files and databases created by an earlier, root-running version are root-owned.
- Set `SERVER_UID` and `SERVER_GID` in `.env` to run as another user, for example the owner of an existing NFS export.
- The key file must be readable by that uid. The `keys` service writes it as `KEYS_UID`:`KEYS_GID`, so keep the two pairs equal.
- On NFS, the uid must be allowed to write to the export (watch for root squash and export ownership).
- **Rollback to running as root:** `SERVER_UID=0` and `SERVER_GID=0` in `.env` are not enough on their own. All capabilities stay dropped (`cap_drop: [ALL]`), so root no longer has `CAP_DAC_OVERRIDE` and cannot read or write files owned by uid 1000. Also give the files back, `chown -R 0:0 <data dir> <storage dir>` (and the key directory, see below), or temporarily add `cap_add: [DAC_OVERRIDE]` to the `server` service. The read-only root filesystem stays in place. `KEYS_UID`/`KEYS_GID` must match the owner of `keys/keys.toml`, otherwise the `keys` service cannot update the file; the same applies to the `server` reading it.

### Limits
- `MAX_UPLOAD_BYTES` (default 256 MiB): a `PUT` is streamed to a temporary file and answered with `413` above this size, judged first by `Content-Length` and then by the bytes actually received. Nothing is left behind and no state row is written.
- Event streams (`GET .../events`): at most 10 concurrent streams per authenticated client name (`429` beyond that); each stream has a queue of 100 events. A client that does not keep up has its stream closed and has to reconnect and reconcile.
- Paths: each component at most 230 UTF-8 bytes (an archive name adds `_<timestamp>` and a counter), the whole path at most 3500 bytes. Components starting with `.syncme-upload-` are reserved for in-flight uploads and rejected. A path that collides with an existing file or directory gives `409`.
- `If-Match` accepts `3`, `"3"` and `W/"3"` (1-18 digits); anything else gives `400`. `since_revision` must be between 0 and 2^62.

### Known limits
- A failure of the SQLite commit AFTER a successful `os.replace` leaves the new file in place while the state row is rolled back. The next restart re-indexes the file as a new revision.
- `requirements.txt` is no longer used by the Docker image: `pyproject.toml` and `uv.lock` are the source of the dependencies.
- `POST .../archive/restore` accepts a JSON body of at most 64 KiB (`413` above that) and checks the key before it reads the body.

### Reverse proxy (HAProxy)
*Not tested here: these snippets are a starting point for the proxy in front of the server.*

```haproxy
frontend syncme
    # the server logs the first X-Forwarded-For entry of failed logins: do not trust one sent by the client
    http-request del-header X-Forwarded-For
    option forwardfor

    # outer upload limit (the server enforces MAX_UPLOAD_BYTES itself)
    http-request deny deny_status 413 if { req.body_size gt 268435456 }

    # per-source-IP rate limit
    stick-table type ip size 100k expire 10m store http_req_rate(10s)
    http-request track-sc0 src
    http-request deny deny_status 429 if { sc_http_req_rate(0) gt 100 }
```

---

## API Reference

All protected endpoints require `Authorization: Bearer <client key>`. The old query parameter `?token=` is not supported and returns `401`.

| Method | Endpoint | Headers / Parameters | Description | Status Codes |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/health`<br>`/api/v1/health` | None | Unauthenticated health check probe. | `200`, `503` |
| `GET` | `/api/v1/workspaces/{id}/sync/state` | `since_revision` (Query, int, default: `0`) | Returns file metadata records for workspace with revision > `since_revision` and `latest_revision`. | `200`, `400`, `401` |
| `GET` | `/api/v1/workspaces/{id}/files/{path}` | URL path | Downloads active file payload. Returns binary stream with `ETag: "<revision>"`. | `200`, `401`, `404` |
| `PUT` | `/api/v1/workspaces/{id}/files/{path}` | `If-Match: "<rev>"` (Header, optional), Body: bytes | Uploads raw file body into workspace (max 256 MiB). Returns `{"revision": <int>}`. | `200`, `401`, `409`, `413` |
| `DELETE` | `/api/v1/workspaces/{id}/files/{path}` | `If-Match: "<rev>"` (Header, optional) | Archives file to workspace safety archive, marks deleted in DB. Returns `{"revision": <int>}`. | `200`, `401`, `404`, `409` |
| `GET` | `/api/v1/workspaces/{id}/events` | None | SSE stream for real-time workspace mutations (`file_updated`, `file_deleted`). Max 10 streams per client. | `200`, `401`, `429` |
| `GET` | `/api/v1/workspaces/{id}/archive` | None | Lists safe-archived disk files for workspace with filenames, sizes, and timestamps. | `200`, `401` |
| `POST` | `/api/v1/workspaces/{id}/archive/restore` | JSON Body: `{"archive_filename": "...", "target_path": "...", "expected_revision": <optional int>}` | Restores archived file back to active workspace storage and broadcasts update event. | `200`, `401`, `404`, `409` |
| `GET` | `/api/v1/workspaces/{id}/history/{path}` | `limit` (Query, int, default: `50`, max: `1000`) | Returns version history for file (`{"path": "...", "history": [{"revision": ..., "sha256": ..., "size": ..., ...}]}`). | `200`, `400`, `401` |
| `GET` | `/api/v1/workspaces/{id}/revisions/{rev}/{path}` | URL path | Downloads historical file content from CAS for specified revision. Returns `ETag: "<sha256>"`. | `200`, `401`, `404` |
| `POST` | `/api/v1/workspaces/{id}/history/{path}/restore` | JSON Body: `{"restore_revision": <int>, "expected_revision": <optional int>}` | Restores historical version from CAS to active HEAD and broadcasts update event (max body 64 KiB). | `200`, `401`, `404`, `409`, `413` |

---

## Development & Testing

SyncMe-Server uses `pytest` and `pytest-asyncio` for test execution.

Run the test suite:
```bash
uv run pytest
# or with active virtual environment:
pytest tests/ -v
```
