from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import GatewayConfig
from app.main import create_app


def make_config(database: Path, **overrides: Any) -> GatewayConfig:
    raw: dict[str, Any] = {
        "api_keys": ["test-key"],
        "database_url": str(database),
        "structured_output_retries": 1,
        "retry": {"max_attempts_per_route": 2, "base_delay_seconds": 0, "max_delay_seconds": 0},
        "circuit_breaker": {"failure_threshold": 5, "cooldown_seconds": 30},
        "rate_limit": {"enabled": False, "requests_per_minute": 100, "burst": 100},
        "providers": {
            "primary": {"base_url": "https://primary.test", "api_key": "upstream-a"},
            "fallback": {"base_url": "https://fallback.test", "api_key": "upstream-b"},
        },
        "models": {
            "smart": {
                "strategy": "priority",
                "routes": [
                    {"provider": "primary", "model": "primary-model", "api": "both"},
                    {"provider": "fallback", "model": "fallback-model", "api": "both"},
                ],
            }
        },
        "pricing": {
            "primary-model": {"input_per_million": 1, "output_per_million": 2},
            "fallback-model": {"input_per_million": 3, "output_per_million": 4},
        },
    }
    raw.update(overrides)
    return GatewayConfig.model_validate(raw)


@asynccontextmanager
async def gateway_client(
    config: GatewayConfig,
    handler: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[httpx.AsyncClient]:
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(config, http_client=upstream_client)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            yield client
    await upstream_client.aclose()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-key"}
