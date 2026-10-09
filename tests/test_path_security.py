import asyncio
import contextlib
import os
import pathlib
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app import db, events, storage
from app.config import settings
from app.main import app

H = {"Authorization": "Bearer test-key-0123456789abcdef0123456789abcdef"}


@pytest.fixture(autouse=True)
def setup_teardown():
    settings.DATA_DIR = Path("./test_data")
    if settings.DATA_DIR.exists():
        shutil.rmtree(settings.DATA_DIR)
    settings.DATA_DIR.mkdir(parents=True)
    import app.db as db

    if hasattr(db._local, "conn"):
        del db._local.conn
    yield
    if settings.DATA_DIR.exists():
        shutil.rmtree(settings.DATA_DIR)


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_storage_rejects_dotdot_workspace():
    with pytest.raises(HTTPException) as exc:
        storage.get_workspace_dir("..")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        storage.get_archive_dir("..")


def test_storage_rejects_workspace_root_as_file_path():
    with pytest.raises(HTTPException) as exc:
        storage.get_file_path("ws1", ".")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [".hidden", "a" * 65])
async def test_route_rejects_bad_workspace(client, bad):
    r = await client.get(f"/api/v1/workspaces/{bad}/sync/state", headers=H)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_bad_workspace_is_checked_after_auth(client):
    r = await client.get("/api/v1/workspaces/.hidden/sync/state")
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["a%5Cb", "a%00b", "/etc/passwd"])
async def test_put_rejects_bad_path(client, bad):
    r = await client.put(f"/api/v1/workspaces/ws1/files/{bad}", content=b"x", headers=H)
    assert r.status_code == 400
    assert not (settings.DATA_DIR / "files" / "workspaces" / "ws1").exists() or not any(
        (settings.DATA_DIR / "files" / "workspaces" / "ws1").rglob("*")
    )


@pytest.mark.asyncio
async def test_put_normalizes_path_for_db_disk_and_state(client):
    r = await client.put("/api/v1/workspaces/ws1/files/a//b.txt", content=b"x", headers=H)
    assert r.status_code == 200
    state = (await client.get("/api/v1/workspaces/ws1/sync/state", headers=H)).json()
    assert [f["path"] for f in state["files"]] == ["a/b.txt"]
    g = await client.get("/api/v1/workspaces/ws1/files/a/b.txt", headers=H)
    assert g.status_code == 200 and g.content == b"x"


@pytest.mark.asyncio
async def test_restore_rejects_traversal(client):
    await client.put("/api/v1/workspaces/ws1/files/a.txt", content=b"x", headers=H)
    await client.delete("/api/v1/workspaces/ws1/files/a.txt", headers=H)
    archive = (await client.get("/api/v1/workspaces/ws1/archive", headers=H)).json()["files"]
    name = archive[0]["archive_filename"]
    for body in (
        {"archive_filename": "../../x", "target_path": "a.txt"},
        {"archive_filename": name, "target_path": "../escape.txt"},
        {"archive_filename": name, "target_path": "/etc/x"},
    ):
        r = await client.post("/api/v1/workspaces/ws1/archive/restore", json=body, headers=H)
        assert r.status_code == 400, body
    ok = await client.post(
        "/api/v1/workspaces/ws1/archive/restore",
        json={"archive_filename": name, "target_path": "a.txt"},
        headers=H,
    )
    assert ok.status_code == 200


# --- Route-level guards: every workspace route must reject an invalid id -----------------

BAD_WS = ".hidden"


def _spied(monkeypatch, target, name):
    """Make the handler's first downstream call blow up, to prove the handler body never runs."""

    def boom(*args, **kwargs):
        raise AssertionError(f"handler body entered for an invalid workspace id ({name})")

    monkeypatch.setattr(target, name, boom)


# (route name, method, url suffix, request kwargs, object and attribute the handler calls first)
WORKSPACE_ROUTES = [
    ("get_state", "GET", "/sync/state", {}, (db, "get_state")),
    ("get_file", "GET", "/files/a.txt", {}, (db, "get_file")),
    ("put_file", "PUT", "/files/a.txt", {"content": b"x"}, (storage, "get_file_path")),
    ("delete_file", "DELETE", "/files/a.txt", {}, (db, "delete_file")),
    ("sse_events", "GET", "/events", {}, (events.broadcaster, "subscribe")),
    ("list_archive", "GET", "/archive", {}, (storage, "list_archive")),
    (
        "restore_archive",
        "POST",
        "/archive/restore",
        {"json": {"archive_filename": "a.txt_1", "target_path": "a.txt"}},
        (storage, "restore_file"),
    ),
    ("get_history", "GET", "/history/a.txt", {}, (db, "get_file_history")),
    ("get_history_revision", "GET", "/history/a.txt/revisions/1", {}, (db, "get_file_version")),
    (
        "restore_history",
        "POST",
        "/history/a.txt/restore",
        {"json": {"restore_revision": 1}},
        (storage, "restore_cas_file"),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", WORKSPACE_ROUTES, ids=[r[0] for r in WORKSPACE_ROUTES])
async def test_every_workspace_route_rejects_invalid_id_before_the_handler_runs(client, monkeypatch, route):
    name, method, suffix, kwargs, (target, attr) = route
    _spied(monkeypatch, target, attr)
    # Bounded, so that a route which lost its dependency fails instead of streaming forever.
    r = await asyncio.wait_for(
        client.request(method, f"/api/v1/workspaces/{BAD_WS}{suffix}", headers=H, **kwargs), timeout=5
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid workspace id"
    assert events.broadcaster.workspaces == {}


def test_every_workspace_route_is_covered_by_the_table():
    from fastapi.routing import APIRoute

    from app.main import app as fastapi_app
    from app.validation import valid_workspace

    found = set()
    for route in fastapi_app.routes:
        if isinstance(route, APIRoute) and "{workspace_id}" in route.path:
            assert valid_workspace in [d.call for d in route.dependant.dependencies], route.path
            found.update(route.endpoint.__name__ for _ in route.methods)
    assert found == {r[0] for r in WORKSPACE_ROUTES}


@pytest.mark.asyncio
async def test_percent_encoded_dotdot_workspace_id_is_rejected_over_http(client):
    g = await client.get("/api/v1/workspaces/%2e%2e/sync/state", headers=H)
    assert g.status_code == 400 and g.json()["detail"] == "Invalid workspace id"
    p = await client.put("/api/v1/workspaces/%2e%2e/files/a.txt", content=b"x", headers=H)
    assert p.status_code == 400 and p.json()["detail"] == "Invalid workspace id"
    assert not any(settings.DATA_DIR.rglob("a.txt"))


# --- Route-level guards: paths are normalized in get_file, delete_file and restore -------


@contextlib.contextmanager
def subscribed(workspace_id):
    queue = events.broadcaster.subscribe(workspace_id, "test-subscriber")
    try:
        yield queue
    finally:
        events.broadcaster.unsubscribe(workspace_id, "test-subscriber")


@pytest.mark.asyncio
async def test_get_normalizes_path(client):
    await client.put("/api/v1/workspaces/ws1/files/a/b.txt", content=b"x", headers=H)
    r = await client.get("/api/v1/workspaces/ws1/files/a//b.txt", headers=H)
    assert r.status_code == 200 and r.content == b"x"


@pytest.mark.asyncio
async def test_get_rejects_bad_path(client):
    r = await client.get("/api/v1/workspaces/ws1/files/a%5Cb", headers=H)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_normalizes_path_and_broadcasts_the_normalized_one(client):
    await client.put("/api/v1/workspaces/ws1/files/a/b.txt", content=b"x", headers=H)
    with subscribed("ws1") as queue:
        r = await client.delete("/api/v1/workspaces/ws1/files/a//b.txt", headers=H)
        assert r.status_code == 200
        msg = queue.get_nowait()
    assert (msg["event"], msg["path"]) == ("file_deleted", "a/b.txt")
    state = (await client.get("/api/v1/workspaces/ws1/sync/state", headers=H)).json()
    assert [(f["path"], f["is_deleted"]) for f in state["files"]] == [("a/b.txt", 1)]


@pytest.mark.asyncio
async def test_delete_rejects_bad_path(client):
    r = await client.delete("/api/v1/workspaces/ws1/files/a%5Cb", headers=H)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_restore_stores_and_broadcasts_the_normalized_target(client):
    await client.put("/api/v1/workspaces/ws1/files/a.txt", content=b"x", headers=H)
    await client.delete("/api/v1/workspaces/ws1/files/a.txt", headers=H)
    name = (await client.get("/api/v1/workspaces/ws1/archive", headers=H)).json()["files"][0]["archive_filename"]
    with subscribed("ws1") as queue:
        r = await client.post(
            "/api/v1/workspaces/ws1/archive/restore",
            json={"archive_filename": name, "target_path": "x//y.txt"},
            headers=H,
        )
        assert r.status_code == 200
        msg = queue.get_nowait()
    assert (msg["event"], msg["path"]) == ("file_updated", "x/y.txt")
    state = (await client.get("/api/v1/workspaces/ws1/sync/state", headers=H)).json()
    assert "x/y.txt" in [f["path"] for f in state["files"]]
    assert "x//y.txt" not in [f["path"] for f in state["files"]]


# --- Symlinks inside the workspace that point outside it ---------------------------------


@pytest.mark.asyncio
async def test_put_through_symlinked_dir_that_escapes_the_workspace_is_rejected(client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.get_workspace_dir("ws1") / "link").symlink_to(outside, target_is_directory=True)
    r = await client.put("/api/v1/workspaces/ws1/files/link/evil.txt", content=b"pwned", headers=H)
    assert r.status_code == 400
    assert list(outside.iterdir()) == []
    state = (await client.get("/api/v1/workspaces/ws1/sync/state", headers=H)).json()
    assert state["files"] == []


@pytest.mark.asyncio
async def test_put_through_symlinked_file_that_escapes_the_workspace_is_rejected(client, tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"original")
    (storage.get_workspace_dir("ws1") / "f.txt").symlink_to(target)
    r = await client.put("/api/v1/workspaces/ws1/files/f.txt", content=b"pwned", headers=H)
    assert r.status_code == 400
    assert target.read_bytes() == b"original"
    state = (await client.get("/api/v1/workspaces/ws1/sync/state", headers=H)).json()
    assert state["files"] == []


# --- Startup indexer must apply the same validation --------------------------------------


def _workspace_dir(name="ws1"):
    d = storage.get_files_dir() / "workspaces" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _indexed():
    rows = db.get_conn().execute("SELECT workspace_id, path FROM files").fetchall()
    return sorted((r["workspace_id"], r["path"]) for r in rows)


def _index():
    return db.index_existing_files(storage.get_files_dir())


def test_indexer_indexes_valid_files_as_before(capsys):
    ws = _workspace_dir()
    (ws / "top.txt").write_bytes(b"1")
    (ws / "sub" / "deep").mkdir(parents=True)
    (ws / "sub" / "deep" / "f.txt").write_bytes(b"2")
    assert _index() == 2
    assert _indexed() == [("ws1", "sub/deep/f.txt"), ("ws1", "top.txt")]
    assert "WARNING" not in capsys.readouterr().out


def test_indexer_skips_workspace_dirs_with_invalid_names_and_warns(capsys):
    _workspace_dir("ws1").joinpath("ok.txt").write_bytes(b"1")
    _workspace_dir(".hidden").joinpath("f.txt").write_bytes(b"2")
    _workspace_dir("a" * 65).joinpath("f.txt").write_bytes(b"3")
    assert _index() == 1
    assert _indexed() == [("ws1", "ok.txt")]
    out = capsys.readouterr().out
    assert ".hidden" in out and "a" * 65 in out


def test_indexer_skips_paths_that_are_not_normalized_and_warns(capsys):
    ws = _workspace_dir()
    (ws / "ok.txt").write_bytes(b"1")
    (ws / "a\\b.txt").write_bytes(b"2")
    (ws / "d\\e").mkdir()
    (ws / "d\\e" / "f.txt").write_bytes(b"3")
    assert _index() == 1
    assert _indexed() == [("ws1", "ok.txt")]
    out = capsys.readouterr().out
    assert "a\\b.txt" in out and "d\\e/f.txt" in out


def test_indexer_skips_paths_that_normalize_to_something_else(capsys, monkeypatch):
    ws = _workspace_dir()
    (ws / "ok.txt").write_bytes(b"1")
    (ws / "odd.txt").write_bytes(b"2")
    import app.validation as validation

    real = validation.normalize_path
    monkeypatch.setattr(db, "normalize_path", lambda rel: "odd.txt/" if rel == "odd.txt" else real(rel))
    assert _index() == 1
    assert _indexed() == [("ws1", "ok.txt")]
    assert "odd.txt" in capsys.readouterr().out


def test_indexer_skips_symlinks_that_escape_the_workspace_without_reading_them(capsys, monkeypatch, tmp_path):
    ws = _workspace_dir()
    (ws / "ok.txt").write_bytes(b"1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    (ws / "leak.txt").symlink_to(outside / "secret.txt")
    (ws / "leakdir").symlink_to(outside, target_is_directory=True)
    (ws / "inner").mkdir()
    (ws / "inner" / "leak2.txt").symlink_to(outside / "secret.txt")

    read = []
    real_read_bytes = pathlib.Path.read_bytes

    def spy(self):
        read.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", spy)
    assert _index() == 1
    assert _indexed() == [("ws1", "ok.txt")]
    assert [p.name for p in read] == ["ok.txt"]
    out = capsys.readouterr().out
    assert "leak.txt" in out and "inner/leak2.txt" in out


def test_indexer_does_not_raise_on_names_that_are_not_utf8(capsys):
    ws = _workspace_dir()
    (ws / "ok.txt").write_bytes(b"1")
    bad_file = os.fsdecode(b"bad\xff.txt")
    bad_ws = os.fsdecode(b"ws\xff")
    try:
        (ws / bad_file).write_bytes(b"2")
        _workspace_dir(bad_ws).joinpath("f.txt").write_bytes(b"3")
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 names")
    assert _index() == 1
    assert _indexed() == [("ws1", "ok.txt")]
    out = capsys.readouterr().out
    assert "bad" in out and "ws" in out
    out.encode("utf-8")  # a real stdout would raise here if a lone surrogate had been printed
