import json

import pytest
from httpx import ASGITransport, AsyncClient

from app import storage
from app.main import RESTORE_MAX_BODY_BYTES, app

KEY = "test-key-0123456789abcdef0123456789abcdef"
H = {"Authorization": f"Bearer {KEY}"}
UP = "/api/v1/workspaces/ws1/files/"
RESTORE = "/api/v1/workspaces/ws1/archive/restore"
ARCHIVE = "/api/v1/workspaces/ws1/archive"
JSON_CT = {"content-type": "application/json"}


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def archived_name(client):
    await client.put(UP + "a.txt", content=b"ORIGINAL", headers=H)
    await client.delete(UP + "a.txt", headers=H)
    return (await client.get(ARCHIVE, headers=H)).json()["files"][0]["archive_filename"]


def body_of_size(archive_filename, size):
    fields = {"archive_filename": archive_filename, "target_path": "a.txt", "client_id": ""}
    pad = size - len(json.dumps(fields).encode())
    fields["client_id"] = "x" * pad
    body = json.dumps(fields).encode()
    assert len(body) == size
    return body


def test_the_body_cap_is_64_kib():
    assert RESTORE_MAX_BODY_BYTES == 64 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-key"}])
@pytest.mark.parametrize("body", [b"{not json", b"x" * (1024 * 1024), b'{"archive_filename": 5}'])
async def test_unauthenticated_restore_is_401_whatever_the_body(client, headers, body):
    r = await client.post(RESTORE, content=body, headers={**JSON_CT, **headers})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_restore_does_not_read_the_body(raw_asgi):
    reads = []
    status, _ = await raw_asgi(
        "POST", RESTORE, {"content-type": "application/json", "content-length": "1048576"},
        chunks=[b"x" * 65536] * 16, on_receive=lambda: reads.append(1),
    )
    assert status == 401 and reads == []


@pytest.mark.asyncio
async def test_a_declared_length_over_the_cap_is_413_before_the_body_is_read(raw_asgi):
    reads = []
    status, _ = await raw_asgi(
        "POST", RESTORE,
        {"authorization": f"Bearer {KEY}", "content-type": "application/json", "content-length": "1048576"},
        chunks=[b"x" * 65536] * 16, on_receive=lambda: reads.append(1),
    )
    assert status == 413 and reads == []


@pytest.mark.asyncio
async def test_a_streamed_body_over_the_cap_is_413(raw_asgi):
    status, _ = await raw_asgi(           # no Content-Length: chunked
        "POST", RESTORE, {"authorization": f"Bearer {KEY}", "content-type": "application/json"},
        chunks=[b"x" * 40000, b"x" * 40000],
    )
    assert status == 413


@pytest.mark.asyncio
async def test_an_authenticated_body_over_the_cap_is_413(client):
    r = await client.post(RESTORE, content=b"x" * (RESTORE_MAX_BODY_BYTES + 1), headers={**H, **JSON_CT})
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_a_body_of_exactly_the_cap_is_accepted_declared_and_streamed(client, raw_asgi):
    name = await archived_name(client)
    exact = body_of_size(name, RESTORE_MAX_BODY_BYTES)
    r = await client.post(RESTORE, content=exact, headers={**H, **JSON_CT})
    assert r.status_code == 200
    status, _ = await raw_asgi(           # chunked: the same body without a Content-Length
        "POST", RESTORE, {"authorization": f"Bearer {KEY}", "content-type": "application/json"},
        chunks=[exact[:30000], exact[30000:]],
    )
    assert status == 200
    over = body_of_size(name, RESTORE_MAX_BODY_BYTES + 1)
    assert (await client.post(RESTORE, content=over, headers={**H, **JSON_CT})).status_code == 413
    status, _ = await raw_asgi(
        "POST", RESTORE, {"authorization": f"Bearer {KEY}", "content-type": "application/json"},
        chunks=[over[:30000], over[30000:]],
    )
    assert status == 413


@pytest.mark.asyncio
async def test_a_valid_restore_still_works(client):
    name = await archived_name(client)
    r = await client.post(RESTORE, json={"archive_filename": name, "target_path": "a.txt"}, headers=H)
    assert r.status_code == 200 and r.json()["revision"] >= 1
    assert (await client.get(UP + "a.txt", headers=H)).content == b"ORIGINAL"


@pytest.mark.asyncio
async def test_a_restore_body_split_over_several_chunks_is_parsed(raw_asgi, client):
    name = await archived_name(client)
    body = json.dumps({"archive_filename": name, "target_path": "b.txt"}).encode()
    status, _ = await raw_asgi(
        "POST", RESTORE, {"authorization": f"Bearer {KEY}", "content-type": "application/json"},
        chunks=[body[:10], body[10:20], body[20:]],
    )
    assert status == 200
    assert (await client.get(UP + "b.txt", headers=H)).content == b"ORIGINAL"


@pytest.mark.asyncio
async def test_an_invalid_field_is_422_with_the_validation_errors(client):
    r = await client.post(RESTORE, json={"archive_filename": 5}, headers=H)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert {(tuple(e["loc"]), e["type"]) for e in detail} >= {
        (("body", "archive_filename"), "string_type"), (("body", "target_path"), "missing"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"{not json", b"", b"\xff\xfe", b"[]"])
async def test_invalid_json_is_422_when_authenticated(client, body):
    r = await client.post(RESTORE, content=body, headers={**H, **JSON_CT})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list) and r.json()["detail"]


@pytest.mark.asyncio
async def test_a_non_ascii_content_length_is_ignored_not_a_500(raw_asgi):
    body = json.dumps({"archive_filename": "nothing_1", "target_path": "a.txt"}).encode()
    status, _ = await raw_asgi(
        "POST", RESTORE,
        {"authorization": f"Bearer {KEY}", "content-type": "application/json", "content-length": "²"},
        chunks=[body],
    )
    assert status == 404                  # parsed and looked up: "Archive file not found"


@pytest.mark.asyncio
@pytest.mark.parametrize("digits", [19, 25, 5000])
async def test_an_absurdly_long_content_length_is_413_not_a_500(raw_asgi, digits):
    # int() refuses strings of more than 4300 digits (ValueError); a number this large is simply "too large"
    status, _ = await raw_asgi(
        "POST", RESTORE,
        {"authorization": f"Bearer {KEY}", "content-type": "application/json", "content-length": "9" * digits},
        chunks=[b"{}"],
    )
    assert status == 413


@pytest.mark.asyncio
async def test_restore_archive_records_client_name(client):
    from app import db
    name = await archived_name(client)
    r = await client.post(RESTORE, json={"archive_filename": name, "target_path": "a.txt"}, headers=H)
    assert r.status_code == 200
    rev = r.json()["revision"]
    version = db.get_file_version("ws1", "a.txt", rev)
    assert version is not None
    assert version["client_name"] == "testclient"

