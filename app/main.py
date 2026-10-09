import asyncio
import hashlib
import logging
import time
import uuid
from typing import Annotated
from fastapi.exceptions import RequestValidationError
from fastapi import FastAPI, Depends, HTTPException, status, Request, UploadFile, File, Header, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel, ValidationError

from . import db, storage, events, auth
from .config import settings
from .keys import keystore
from .logging_config import configure_logging
from .validation import normalize_path, parse_if_match, valid_workspace

from contextlib import asynccontextmanager

log = logging.getLogger("syncme.health")

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    keystore.load()   # fail closed: a missing or invalid key file stops the server
    storage.sweep_stale_temp_files()   # orphans of a crashed upload; before indexing, which skips them anyway
    # Startup: index existing files in storage dir in background so server starts immediately
    index_task = asyncio.create_task(asyncio.to_thread(db.index_existing_files, storage.get_files_dir()))
    def _on_index_done(t):
        if not t.cancelled() and t.exception():
            logging.getLogger("syncme.index").error("Startup indexing failed: %s", t.exception(), exc_info=t.exception())
    index_task.add_done_callback(_on_index_done)
    yield
    await index_task

# An event stream is authenticated when it connects; it is re-checked this often (seconds) and whenever
# an event is due, and ends when its key no longer maps to the same client. Read at call time.
SSE_REVALIDATE_INTERVAL = 15

# The interactive docs and the OpenAPI schema would be served without a key: switch them off.
app = FastAPI(title="SyncMe Server", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

def _declared_length(request: Request) -> int | None:
    """The Content-Length as a number, or None when it is absent or not a plain ASCII number."""
    value = request.headers.get("content-length")
    # isdigit() alone accepts non-ASCII digits (e.g. superscript two) that int() rejects
    if value is None or not (value.isascii() and value.isdigit()):
        return None
    # int() raises ValueError above 4300 digits; anything past 18 digits exceeds every limit anyway
    return int(value) if len(value) <= 18 else 10**18


@app.get("/health")
@app.get("/api/v1/health")
def health():
    try:
        conn = db.get_conn()
        conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception:
        log.exception("health check failed")   # the detail goes to the log, never to the caller
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

@app.get("/api/v1/workspaces/{workspace_id}/sync/state", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
@app.get("/api/v1/workspaces/{workspace_id}/files", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
def get_state(
    workspace_id: str,
    request: Request,
    client_name: Annotated[str, Depends(auth.verify_token)],
    since_revision: Annotated[int, Query(ge=0, le=2**62)] = 0,
):
    files, latest_revision = db.get_state(workspace_id, since_revision)
    db.record_client_activity(client_name, workspace_id, revision=since_revision, ip=auth._source_ip(request))
    return {"files": files, "latest_revision": latest_revision, "last_revision": latest_revision}

get_files = get_state

@app.get("/api/v1/workspaces/{workspace_id}/files/{path:path}", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
def get_file(workspace_id: str, path: str):
    path = normalize_path(path)
    file_record = db.get_file(workspace_id, path)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found in state")
    if file_record["is_deleted"]:
        raise HTTPException(status_code=404, detail="File is deleted")
        
    file_path = storage.get_file_path(workspace_id, path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
        
    return FileResponse(file_path, headers={"ETag": str(file_record["revision"])})

@app.put("/api/v1/workspaces/{workspace_id}/files/{path:path}", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
async def put_file(
    workspace_id: str,
    path: str,
    request: Request,
    client_name: Annotated[str, Depends(auth.verify_token)],
    if_match: Annotated[str | None, Header()] = None,
):
    path = normalize_path(path)
    expected_revision = parse_if_match(if_match)
    
    import os
    import tempfile
    from .validation import RESERVED_PREFIX

    limit = settings.MAX_UPLOAD_BYTES
    declared = _declared_length(request)
    if declared is not None and declared > limit:
        raise HTTPException(status_code=413, detail="Upload too large")

    file_path = storage.get_file_path(workspace_id, path)
    digest = hashlib.sha256()
    size = 0
    # The temp file goes in the workspace root (reserved prefix, same filesystem as the target), not next to
    # the target: the target's directories are created only when the row is about to be committed, so an
    # upload that is rejected (413, revision conflict, 409) leaves neither a directory nor a temp file behind.
    tmp = tempfile.NamedTemporaryFile(dir=storage.get_workspace_dir(workspace_id), delete=False, prefix=RESERVED_PREFIX)
    tmp_name = tmp.name

    def commit_file():
        file_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_name, file_path)

    try:
        with tmp:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail="Upload too large")
                digest.update(chunk)
                tmp.write(chunk)
            tmp.flush()
            os.fsync(tmp.fileno())          # the data is on disk before the rename makes it the file
            
        sha256_hex = digest.hexdigest()
        storage.store_cas_object(workspace_id, tmp_name, sha256_hex)
        
        new_rev = db.upsert_file(
            workspace_id, path, sha256_hex, size, time.time(), expected_revision,
            before_commit=commit_file, client_name=client_name
        )
    except (IsADirectoryError, NotADirectoryError, FileExistsError):
        raise HTTPException(status_code=409, detail="Path conflicts with an existing file or directory")
    finally:
        try:
            os.unlink(tmp_name)          # only still there if the replace did not happen
        except FileNotFoundError:
            pass

    await events.broadcaster.broadcast(workspace_id, "file_updated", path, new_rev, client_name)
    db.record_client_activity(client_name, workspace_id, revision=new_rev, ip=auth._source_ip(request))
    return {"revision": new_rev}

upload_file = put_file

@app.delete("/api/v1/workspaces/{workspace_id}/files/{path:path}", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
async def delete_file(
    workspace_id: str,
    path: str,
    request: Request,
    client_name: Annotated[str, Depends(auth.verify_token)],
    if_match: Annotated[str | None, Header()] = None,
):
    path = normalize_path(path)
    expected_revision = parse_if_match(if_match)
    
    new_rev = db.delete_file(workspace_id, path, expected_revision, client_name=client_name)
    await events.broadcaster.broadcast(workspace_id, "file_deleted", path, new_rev, client_name)
    db.record_client_activity(client_name, workspace_id, revision=new_rev, ip=auth._source_ip(request))
    
    return {"revision": new_rev}

@app.get("/api/v1/workspaces/{workspace_id}/events", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
async def sse_events(
    workspace_id: str,
    request: Request,
    client_name: Annotated[str, Depends(auth.verify_token)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(auth.security)],
):
    db.record_client_activity(client_name, workspace_id, ip=auth._source_ip(request))
    token = credentials.credentials if credentials else ""
    client_id = str(uuid.uuid4())
    try:
        queue = events.broadcaster.subscribe(workspace_id, client_id, owner=client_name)
    except events.TooManySubscribers:
        raise HTTPException(status_code=429, detail="Too many event streams for this client")
    
    async def event_generator():
        try:
            while True:
                if queue.dropped:
                    return          # overflowed: end the stream, the client reconnects and reconciles
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=SSE_REVALIDATE_INTERVAL)
                except asyncio.TimeoutError:
                    msg = None
                # The key was checked once, at connect. Check it again on every wake-up (timer or event)
                # so that a revoked or rotated key does not keep a stream open.
                if await asyncio.to_thread(keystore.authenticate, token) != client_name:
                    return
                if msg is None:
                    db.record_client_activity(client_name, workspace_id, ip=auth._source_ip(request))
                    yield {"comment": "keepalive"}
                    continue
                import json
                yield {
                    "event": "message",
                    "data": json.dumps(msg)
                }
        finally:
            events.broadcaster.unsubscribe(workspace_id, client_id)
            
    return EventSourceResponse(event_generator())

@app.get("/api/v1/workspaces/{workspace_id}/archive", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
def list_archive(workspace_id: str):
    return {"files": storage.list_archive(workspace_id)}

class RestoreRequest(BaseModel):
    archive_filename: str
    target_path: str
    expected_revision: int | None = None
    client_id: str | None = None  # ignored: the authenticated client name is used

# A restore request is a few hundred bytes of JSON; anything larger is refused.
RESTORE_MAX_BODY_BYTES = 64 * 1024

@app.post("/api/v1/workspaces/{workspace_id}/archive/restore", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
async def restore_archive(workspace_id: str, request: Request, client_name: Annotated[str, Depends(auth.verify_token)]):
    # The body is NOT a declared parameter: FastAPI would read and validate it before it runs the
    # authentication dependencies, so an unauthenticated caller could make the server buffer any amount of
    # data. Here the key has already been checked; only now is the body read, and only up to the cap.
    declared = _declared_length(request)
    if declared is not None and declared > RESTORE_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > RESTORE_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
    try:
        req = RestoreRequest.model_validate_json(bytes(body))
    except ValidationError as e:
        # the same 422 body FastAPI produces for a declared body parameter
        raise RequestValidationError(
            [{**err, "loc": ("body", *err["loc"])} for err in e.errors(include_url=False, include_context=False, include_input=False)]
        ) from None
    req.target_path = normalize_path(req.target_path)
    try:
        new_rev = storage.restore_file(workspace_id, req.archive_filename, req.target_path, req.expected_revision)
        await events.broadcaster.broadcast(workspace_id, "file_updated", req.target_path, new_rev, client_name)
        return {"revision": new_rev}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Archive file not found")

@app.get("/api/v1/workspaces/{workspace_id}/revisions/{revision}/{path:path}", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
def get_history_revision(workspace_id: str, revision: int, path: str):
    path = normalize_path(path)
    version = db.get_file_version(workspace_id, path, revision)
    if not version:
        raise HTTPException(status_code=404, detail="Revision not found")
    if version.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Revision is deleted")
    sha256 = version.get("sha256")
    if not sha256:
        raise HTTPException(status_code=404, detail="File content not found")
        
    cas_path = storage.get_cas_object(workspace_id, sha256)
    if not cas_path or not cas_path.exists():
        raise HTTPException(status_code=404, detail="File content not found in CAS")
        
    return FileResponse(cas_path, headers={"ETag": f'"{sha256}"'})

class RestoreHistoryRequest(BaseModel):
    restore_revision: int | None = None
    revision: int | None = None
    expected_revision: int | None = None
    client_id: str | None = None

@app.post("/api/v1/workspaces/{workspace_id}/history/{path:path}/restore", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
async def restore_history(workspace_id: str, path: str, request: Request, client_name: Annotated[str, Depends(auth.verify_token)]):
    declared = _declared_length(request)
    if declared is not None and declared > RESTORE_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > RESTORE_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
    try:
        req = RestoreHistoryRequest.model_validate_json(bytes(body))
    except ValidationError as e:
        raise RequestValidationError(
            [{**err, "loc": ("body", *err["loc"])} for err in e.errors(include_url=False, include_context=False, include_input=False)]
        ) from None
    
    rev_to_restore = req.restore_revision if req.restore_revision is not None else req.revision
    if rev_to_restore is None:
        raise RequestValidationError([{"loc": ("body",), "msg": "restore_revision or revision required"}])
        
    path = normalize_path(path)
    try:
        new_rev = storage.restore_cas_file(workspace_id, path, rev_to_restore, req.expected_revision, client_name=client_name)
        await events.broadcaster.broadcast(workspace_id, "file_updated", path, new_rev, client_name)
        return {"path": path, "revision": new_rev, "new_revision": new_rev}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Revision not found")

@app.get("/api/v1/workspaces/{workspace_id}/history/{path:path}", dependencies=[Depends(auth.verify_token), Depends(valid_workspace)])
def get_history(workspace_id: str, path: str, limit: Annotated[int, Query(ge=1, le=1000)] = 50):
    path = normalize_path(path)
    return {"path": path, "history": db.get_file_history(workspace_id, path, limit=limit)}
