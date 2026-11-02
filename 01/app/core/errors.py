from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        error_type: str = "gateway_error",
        code: str | None = None,
        param: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param
        self.details = details


class UpstreamError(GatewayError):
    def __init__(self, message: str, *, status_code: int = 502, retryable: bool = False, details: Any = None):
        super().__init__(message, status_code=status_code, error_type="upstream_error", details=details)
        self.retryable = retryable


class StructuredOutputError(GatewayError):
    def __init__(self, message: str, *, details: Any = None):
        super().__init__(
            message,
            status_code=422,
            error_type="structured_output_error",
            code="invalid_model_output",
            details=details,
        )


def error_payload(error: GatewayError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "message": error.message,
            "type": error.error_type,
            "param": error.param,
            "code": error.code,
        }
    }
    if error.details is not None:
        payload["error"]["details"] = error.details
    return payload


async def gateway_error_handler(_: Request, error: GatewayError) -> JSONResponse:
    headers = {"Retry-After": "1"} if error.status_code == 429 else None
    return JSONResponse(error_payload(error), status_code=error.status_code, headers=headers)
