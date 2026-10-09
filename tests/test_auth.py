import asyncio
import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
from app import events
from app.config import settings
from app.keys import KeyFileError, keystore
from app.main import app

KEY = "test-key-0123456789abcdef0123456789abcdef"
H = {"Authorization": f"Bearer {KEY}"}
STATE = "/api/v1/workspaces/ws1/sync/state"
WRONG = "Bearer wrong-wrong-wrong-wrong-wrong-wrong-wrong"


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    # Keep the tests inside ./test_data even if the environment or a local .env points elsewhere.
    for name in ("STORAGE_DIR", "FILES_DIR", "ARCHIVE_DIR", "DB_PATH"):
        monkeypatch.setattr(settings, name, None)
    settings.DATA_DIR = Path("./test_data")
    if settings.DATA_DIR.exists():
        shutil.rmtree(settings.DATA_DIR)
    settings.DATA_DIR.mkdir(parents=True)
    import app.db as db

    if hasattr(db._local, "conn"):
        del db._local.conn
    yield
    shutil.rmtree(settings.DATA_DIR, ignore_errors=True)


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_valid_key_is_accepted(client):
    assert (await client.get(STATE, headers=H)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Authorization": WRONG}, {"Authorization": "Basic abc"}])
async def test_bad_or_missing_key_is_rejected(client, headers):
    assert (await client.get(STATE, headers=headers)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", STATE),
        ("GET", "/api/v1/workspaces/ws1/files/a.txt"),
        ("PUT", "/api/v1/workspaces/ws1/files/a.txt"),
        ("DELETE", "/api/v1/workspaces/ws1/files/a.txt"),
        ("GET", "/api/v1/workspaces/ws1/events"),
        ("GET", "/api/v1/workspaces/ws1/archive"),
        ("POST", "/api/v1/workspaces/ws1/archive/restore"),
    ],
)
async def test_every_workspace_route_requires_a_valid_key(client, method, url):
    body = {"archive_filename": "x", "target_path": "a.txt"}
    for headers in ({}, {"Authorization": WRONG}):
        # the timeout turns an unprotected SSE route (which never ends) into a failure, not a hang
        r = await asyncio.wait_for(
            client.request(method, url, headers=headers, json=body if method == "POST" else None), 5
        )
        assert r.status_code == 401, (method, url, headers)


@pytest.mark.asyncio
async def test_query_token_is_no_longer_accepted(client):
    assert (await client.get(f"{STATE}?token={KEY}")).status_code == 401
    assert (await client.get("/api/v1/workspaces/ws1/events?token=" + KEY)).status_code == 401


@pytest.mark.asyncio
async def test_empty_keystore_fails_closed(client):
    keystore.set_keys({})
    assert (await client.get(STATE, headers=H)).status_code == 401


@pytest.mark.asyncio
async def test_failed_auth_logs_source_ip_but_no_token(client, caplog):
    with caplog.at_level("WARNING", logger="syncme.auth"):
        await client.get(STATE, headers={"Authorization": WRONG, "X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    assert "203.0.113.9" in caplog.text
    assert "10.0.0.1" not in caplog.text            # only the first entry counts
    assert "wrong-wrong" not in caplog.text and KEY not in caplog.text


@pytest.mark.asyncio
async def test_failed_auth_without_forwarded_header_logs_peer_address(client, caplog):
    with caplog.at_level("WARNING", logger="syncme.auth"):
        await client.get(STATE, headers={"Authorization": WRONG})
        await client.get(STATE, headers={"Authorization": WRONG, "X-Forwarded-For": " "})
    assert caplog.text.count("127.0.0.1") == 2      # peer address of the ASGI test transport


@pytest.mark.asyncio
async def test_forwarded_for_with_newline_cannot_forge_log_lines(client, caplog):
    with caplog.at_level("WARNING", logger="syncme.auth"):
        await client.get(STATE, headers={"X-Forwarded-For": "1.2.3.4\nINFO fake line"})
    assert "authentication failed" in caplog.text    # the request really was logged
    assert "\nINFO fake line" not in caplog.text


@pytest.mark.asyncio
async def test_forwarded_for_is_truncated_in_the_log(client, caplog):
    with caplog.at_level("WARNING", logger="syncme.auth"):
        await client.get(STATE, headers={"X-Forwarded-For": "a" * 200})
    assert "a" * 64 in caplog.text and "a" * 65 not in caplog.text


@pytest.mark.asyncio
async def test_origin_client_id_is_the_authenticated_name_not_the_header(client):
    q = events.broadcaster.subscribe("ws1", "probe")
    try:
        r = await client.put(
            "/api/v1/workspaces/ws1/files/a.txt", content=b"x", headers={**H, "X-Client-ID": "spoofed"}
        )
        assert r.status_code == 200
        assert (await asyncio.wait_for(q.get(), 1))["origin_client_id"] == "testclient"
        r = await client.delete("/api/v1/workspaces/ws1/files/a.txt", headers={**H, "X-Client-ID": "spoofed"})
        assert r.status_code == 200
        assert (await asyncio.wait_for(q.get(), 1))["origin_client_id"] == "testclient"
    finally:
        events.broadcaster.unsubscribe("ws1", "probe")


@pytest.mark.asyncio
async def test_restore_ignores_client_id_in_body(client):
    await client.put("/api/v1/workspaces/ws1/files/a.txt", content=b"x", headers=H)
    await client.delete("/api/v1/workspaces/ws1/files/a.txt", headers=H)
    name = (await client.get("/api/v1/workspaces/ws1/archive", headers=H)).json()["files"][0]["archive_filename"]
    q = events.broadcaster.subscribe("ws1", "probe")
    try:
        r = await client.post(
            "/api/v1/workspaces/ws1/archive/restore",
            json={"archive_filename": name, "target_path": "a.txt", "client_id": "spoofed"},
            headers=H,
        )
        assert r.status_code == 200
        assert (await asyncio.wait_for(q.get(), 1))["origin_client_id"] == "testclient"
    finally:
        events.broadcaster.unsubscribe("ws1", "probe")


@pytest.mark.asyncio
async def test_two_clients_are_told_apart(client):
    keystore.set_keys({"testclient": KEY, "phone": "p" * 40})
    q = events.broadcaster.subscribe("ws1", "probe")
    try:
        await client.put(
            "/api/v1/workspaces/ws1/files/a.txt", content=b"x", headers={"Authorization": "Bearer " + "p" * 40}
        )
        assert (await asyncio.wait_for(q.get(), 1))["origin_client_id"] == "phone"
    finally:
        events.broadcaster.unsubscribe("ws1", "probe")


@pytest.mark.asyncio
async def test_health_stays_unauthenticated(client):
    assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_startup_fails_closed_without_a_key_file(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "configure_logging", lambda: None)
    monkeypatch.setattr(settings, "SYNCME_KEYS_FILE", tmp_path / "missing.toml")
    with pytest.raises(KeyFileError, match="not found"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_startup_loads_the_key_file(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(main_module, "configure_logging", lambda: None)
    path = tmp_path / "keys.toml"
    path.write_text(f'[clients.fromfile]\nkey = "{"f" * 40}"\n')
    os.chmod(path, 0o600)
    monkeypatch.setattr(settings, "SYNCME_KEYS_FILE", path)
    keystore.set_keys({})
    async with app.router.lifespan_context(app):
        assert keystore.authenticate("f" * 40) == "fromfile"


# --- schema and docs routes are not served ----------------------------------------------------------


def test_the_app_registers_no_docs_or_schema_urls():
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["/docs", "/redoc", "/openapi.json"])
async def test_docs_and_schema_routes_are_not_served(client, url):
    assert (await client.get(url)).status_code == 404                  # without a key
    assert (await client.get(url, headers=H)).status_code == 404       # and not with one either


# --- an open event stream ends when its key stops being valid ------------------------------------------

EVENTS = "/api/v1/workspaces/ws1/events"


async def _open_stream(client, headers=H):
    """httpx's ASGI transport buffers the response, so the request task finishes only when the stream ends."""
    task = asyncio.create_task(client.get(EVENTS, headers=headers))
    for _ in range(200):
        if events.broadcaster.workspaces.get("ws1"):
            return task
        await asyncio.sleep(0.01)
    task.cancel()
    pytest.fail("the stream never subscribed")


async def _finish(task, timeout=3):
    try:
        return await asyncio.wait_for(task, timeout)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def fast_revalidation(monkeypatch):
    monkeypatch.setattr(main_module, "SSE_REVALIDATE_INTERVAL", 0.1)


def test_the_revalidation_interval_defaults_to_15_seconds():
    assert main_module.SSE_REVALIDATE_INTERVAL == 15


@pytest.mark.asyncio
async def test_stream_stays_open_while_the_key_is_valid_and_ends_after_a_revoke(client, fast_revalidation):
    task = await _open_stream(client)
    await asyncio.sleep(0.5)                                  # several intervals
    assert not task.done()
    keystore.set_keys({"other": "o" * 40})                    # the client's key is revoked
    r = await _finish(task)
    assert r.status_code == 200
    assert events.broadcaster.workspaces == {}                # and the subscription is gone


@pytest.mark.asyncio
async def test_stream_ends_when_the_key_of_the_same_client_is_rotated(client, fast_revalidation):
    task = await _open_stream(client)
    keystore.set_keys({"testclient": "r" * 40})               # same name, new key: the old key is revoked
    await _finish(task)


@pytest.mark.asyncio
async def test_stream_ends_when_the_key_is_handed_to_a_different_client(client, fast_revalidation):
    task = await _open_stream(client)
    keystore.set_keys({"renamed": KEY})                       # the same key still works, but for someone else
    await _finish(task)


@pytest.mark.asyncio
async def test_stream_survives_the_revocation_of_another_client(client, fast_revalidation):
    keystore.set_keys({"testclient": KEY, "second": "s" * 40})
    task = await _open_stream(client)
    keystore.set_keys({"testclient": KEY})                    # somebody else's key is revoked
    await asyncio.sleep(0.5)
    assert not task.done()
    keystore.set_keys({})                                     # now ours goes too
    await _finish(task)


@pytest.mark.asyncio
async def test_a_valid_stream_delivers_events(client, fast_revalidation):
    task = await _open_stream(client)
    await events.broadcaster.broadcast("ws1", "file_updated", "delivered.txt", 1, "someone")
    await asyncio.sleep(0.3)
    keystore.set_keys({})
    r = await _finish(task)
    assert "delivered.txt" in r.text


@pytest.mark.asyncio
async def test_no_event_is_delivered_after_a_revoke_even_if_the_wake_up_is_a_message(client, monkeypatch):
    monkeypatch.setattr(main_module, "SSE_REVALIDATE_INTERVAL", 30)     # only a message can wake the stream
    task = await _open_stream(client)
    keystore.set_keys({"other": "o" * 40})
    await events.broadcaster.broadcast("ws1", "file_updated", "leaked.txt", 1, "someone")
    r = await _finish(task)
    assert "leaked.txt" not in r.text


# --- an event stream is bounded: per-client limit and a bounded queue -------------------------------------


@pytest.mark.asyncio
async def test_eleventh_stream_for_one_client_is_429(client, monkeypatch):
    monkeypatch.setattr(events, "MAX_SUBSCRIBERS_PER_CLIENT", 2)
    events.broadcaster.subscribe("ws1", "s1", owner="testclient")
    events.broadcaster.subscribe("ws1", "s2", owner="testclient")
    try:
        r = await asyncio.wait_for(client.get(EVENTS, headers=H), 5)
        assert r.status_code == 429
        assert events.broadcaster.count_for("testclient") == 2           # the refused stream left nothing behind
    finally:
        events.broadcaster.unsubscribe("ws1", "s1")
        events.broadcaster.unsubscribe("ws1", "s2")


@pytest.mark.asyncio
async def test_streams_of_other_clients_do_not_count_against_the_limit(client, monkeypatch, fast_revalidation):
    monkeypatch.setattr(events, "MAX_SUBSCRIBERS_PER_CLIENT", 1)
    events.broadcaster.subscribe("ws1", "s1", owner="somebody-else")
    try:
        task = asyncio.create_task(client.get(EVENTS, headers=H))
        for _ in range(200):
            if events.broadcaster.count_for("testclient") == 1:
                break
            await asyncio.sleep(0.01)
        else:
            task.cancel()
            pytest.fail("the stream never subscribed")
        keystore.set_keys({})                                             # end the stream
        assert (await _finish(task)).status_code == 200
    finally:
        events.broadcaster.unsubscribe("ws1", "s1")


@pytest.mark.asyncio
async def test_an_open_stream_is_counted_for_its_client_and_released_when_it_ends(client, fast_revalidation):
    task = await _open_stream(client)
    assert events.broadcaster.count_for("testclient") == 1
    keystore.set_keys({})
    await _finish(task)
    assert events.broadcaster.count_for("testclient") == 0


@pytest.mark.asyncio
async def test_a_dropped_stream_ends(client, monkeypatch, fast_revalidation):
    monkeypatch.setattr(events, "QUEUE_MAXSIZE", 1)
    task = await _open_stream(client)
    for i in range(5):                                       # overflow the one-slot queue
        await events.broadcaster.broadcast("ws1", "file_updated", f"f{i}", i)
    r = await _finish(task, timeout=5)                       # must end, not hang
    assert r.status_code == 200
    assert events.broadcaster.workspaces == {}               # and the subscription is gone
