import re
import unicodedata

from fastapi import HTTPException, status

_WORKSPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# 230, not the filesystem's 255: deleting a file archives it as "<name>_<10 digit timestamp>" (plus "-<n>"
# when that name is taken), so every name that can be uploaded must leave room for that suffix.
MAX_COMPONENT_BYTES = 230
# Well under PATH_MAX (4096): the path on disk also carries the storage directory and the workspace id.
MAX_PATH_BYTES = 3500
RESERVED_PREFIX = ".syncme-upload-"

_IF_MATCH_RE = re.compile(r'"([0-9]{1,18})"|W/"([0-9]{1,18})"|([0-9]{1,18})')   # ASCII digits only
_CONFLICT_ARTIFACT_RE = re.compile(r".+\.conflict\.[0-9]+(?:_[0-9]+)?(?:\.[^/]+)?$")


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def validate_workspace_id(workspace_id: str) -> str:
    if not _WORKSPACE_RE.fullmatch(workspace_id):
        raise _bad("Invalid workspace id")
    if workspace_id in ("objects", "archive"):
        raise _bad("Reserved workspace id")
    return workspace_id


def parse_if_match(value: str | None) -> int | None:
    if value is None:
        return None
    m = _IF_MATCH_RE.fullmatch(value.strip())
    if not m:
        raise _bad("Invalid If-Match header")
    return int(next(g for g in m.groups() if g))


# What an archive name adds to the last component: "_<10 digit timestamp>" and "-<n>" (see storage.archive_file).
ARCHIVE_SUFFIX_BYTES = 255 - MAX_COMPONENT_BYTES


def normalize_path(path: str, slack: int = 0) -> str:
    """Normalize a client-supplied relative path; `slack` widens the length limits (archive names only)."""
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        raise _bad("Invalid path")
    path = unicodedata.normalize("NFC", path)
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise _bad("Invalid path")
    try:
        encoded = [p.encode("utf-8") for p in parts]
    except UnicodeEncodeError:
        raise _bad("Invalid path") from None
    if any(len(e) > MAX_COMPONENT_BYTES + slack for e in encoded) or sum(len(e) for e in encoded) + len(parts) - 1 > MAX_PATH_BYTES + slack:
        raise _bad("Path too long")
    if any(p.startswith(RESERVED_PREFIX) for p in parts):
        raise _bad("Invalid path")
    if slack == 0 and _CONFLICT_ARTIFACT_RE.fullmatch(parts[-1]):
        raise _bad("Invalid path: conflict artifact")
    return "/".join(parts)


def valid_workspace(workspace_id: str) -> str:
    """FastAPI dependency: the parameter name must match the route's {workspace_id}."""
    return validate_workspace_id(workspace_id)
