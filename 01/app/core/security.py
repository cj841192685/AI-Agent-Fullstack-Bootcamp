from __future__ import annotations

import hashlib
import hmac

from fastapi import Header, Request

from app.core.errors import GatewayError


def key_fingerprint(value: str | None) -> str:
    if not value:
        return "anonymous"
    return hashlib.sha256(value.encode()).hexdigest()[:16]


async def authenticate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> str:
    configured = [
        item.get_secret_value() for item in request.app.state.config.api_keys if item.get_secret_value()
    ]
    if not configured:
        return key_fingerprint(request.client.host if request.client else None)

    supplied = x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not any(hmac.compare_digest(supplied, candidate) for candidate in configured):
        raise GatewayError(
            "Invalid or missing API key",
            status_code=401,
            error_type="authentication_error",
            code="invalid_api_key",
        )
    return key_fingerprint(supplied)
