import pytest
from fastapi import HTTPException

from app.validation import MAX_COMPONENT_BYTES, normalize_path, parse_if_match, validate_workspace_id


@pytest.mark.parametrize("ws", ["default", "files", "ws1", "My-Docs_2", "a.b", "a" * 64])
def test_valid_workspace_ids(ws):
    assert validate_workspace_id(ws) == ws


@pytest.mark.parametrize(
    "ws", ["", ".", "..", ".hidden", "a/b", "a\\b", "a b", "a" * 65, "ws\n", "ws\x00", "-x", "objects", "archive"]
)
def test_invalid_workspace_ids(ws):
    with pytest.raises(HTTPException) as exc:
        validate_workspace_id(ws)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a.txt", "a.txt"),
        ("dir/a.txt", "dir/a.txt"),
        ("a//b", "a/b"),
        ("a/./b", "a/b"),
        ("./a", "a"),
        ("a/b/", "a/b"),
    ],
)
def test_normalize_path_ok(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "/", ".", "./", "/etc/passwd", "..", "../x", "a/../b", "a/..", "a\\b", "a\x00b"]
)
def test_normalize_path_rejected(raw):
    with pytest.raises(HTTPException) as exc:
        normalize_path(raw)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("raw,expected", [("3", 3), ('"3"', 3), ('W/"3"', 3), (" 7 ", 7), ("0", 0), (None, None)])
def test_parse_if_match_ok(raw, expected):
    assert parse_if_match(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "", '"3', '3"', "-1", "1.5", "*", "1" * 19, "3, 4", "W/3", "\u0663"])
def test_parse_if_match_rejected(raw):
    with pytest.raises(HTTPException) as exc:
        parse_if_match(raw)
    assert exc.value.status_code == 400


def test_parse_if_match_accepts_18_digits():
    assert parse_if_match("9" * 18) == int("9" * 18)


def test_component_length_limit():
    # 230, not 255: an archive name adds "_<10 digit timestamp>" and "-<n>" to the last component
    assert normalize_path("a" * 230) == "a" * 230
    with pytest.raises(HTTPException):
        normalize_path("a" * 231)
    assert normalize_path("\u00e9" * 115) == "\u00e9" * 115      # 230 bytes in UTF-8
    with pytest.raises(HTTPException):
        normalize_path("\u00e9" * 116)            # 232 bytes in UTF-8


def test_component_limit_leaves_room_for_the_archive_suffix():
    assert MAX_COMPONENT_BYTES + len("_1700000000-999") <= 255


def test_total_path_length_limit():
    ok = "/".join(["a" * 200] * 17)          # 17*200 + 16 = 3416 bytes
    assert normalize_path(ok) == ok
    with pytest.raises(HTTPException):
        normalize_path("/".join(["a" * 200] * 18))      # 3617 bytes


def test_total_path_length_limit_is_exact():
    head = ["a" * 230] * 14
    at_limit = "/".join(head + ["b" * 100, "c" * 165])      # 3220 + 265 bytes + 15 separators = 3500
    assert len(at_limit.encode()) == 3500
    assert normalize_path(at_limit) == at_limit
    with pytest.raises(HTTPException) as exc:
        normalize_path("/".join(head + ["b" * 100, "c" * 166]))   # 3501
    assert exc.value.status_code == 400


def test_total_path_length_is_counted_in_bytes_not_characters():
    comp = "\u00e9" * 114                       # 114 characters, 228 bytes
    wide = "/".join([comp] * 16)                # 1839 characters (under the limit), 3663 bytes
    with pytest.raises(HTTPException) as exc:
        normalize_path(wide)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("raw", ["a\ud800b", ".syncme-upload-x", "dir/.syncme-upload-y/z"])
def test_unencodable_and_reserved_names_rejected(raw):
    with pytest.raises(HTTPException) as exc:
        normalize_path(raw)
    assert exc.value.status_code == 400
