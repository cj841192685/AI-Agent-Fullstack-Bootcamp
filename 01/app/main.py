from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import router
from app.config import GatewayConfig, load_config
from app.core.errors import GatewayError, gateway_error_handler
from app.core.rate_limit import InMemoryConcurrencyLimiter, InMemoryRateLimiter
from app.services.gateway import GatewayService
from app.services.prompts import PromptRepository
from app.services.router import ModelRouter
from app.services.upstream import UpstreamClient
from app.services.usage import UsageRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def create_app(
    config: GatewayConfig | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    gateway_config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database_path = Path(gateway_config.database_url)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        prompts = PromptRepository(str(database_path))
        usage = UsageRepository(str(database_path), gateway_config)
        await prompts.initialize()
        await usage.initialize()
        model_router = ModelRouter(gateway_config)
        upstream = UpstreamClient(gateway_config, http_client)

        app.state.config = gateway_config
        app.state.prompts = prompts
        app.state.usage = usage
        app.state.model_router = model_router
        app.state.upstream = upstream
        app.state.rate_limiter = InMemoryRateLimiter(gateway_config.rate_limit)
        app.state.concurrency_limiter = InMemoryConcurrencyLimiter(
            gateway_config.concurrency.max_concurrent_requests
        )
        app.state.gateway = GatewayService(gateway_config, model_router, upstream, prompts, usage)
        yield
        await upstream.close()

    app = FastAPI(
        title="Unified LLM Gateway",
        version=__version__,
        description="OpenAI-compatible multi-model gateway for Agent Engineering Platforms.",
        lifespan=lifespan,
    )
    app.add_exception_handler(GatewayError, gateway_error_handler)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "message": "Invalid request",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "validation_error",
                    "details": exc.errors(),
                }
            },
            status_code=422,
        )

    app.include_router(router)
    return app


app = create_app()
