import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .keys import keystore

log = logging.getLogger("syncme.auth")
security = HTTPBearer(auto_error=False)


def _source_ip(request: Request) -> str:
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded[:64]
    return (request.client.host if request.client else "-")[:64]


def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Return the name of the client that owns the presented key, or raise 401."""
    name = keystore.authenticate(credentials.credentials) if credentials else None
    if name is None:
        log.warning("authentication failed from %r", _source_ip(request))   # never log the token
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return name
