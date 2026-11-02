from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.errors import GatewayError
from app.core.security import authenticate
from app.schemas import (
    ChatCompletionRequest,
    ModelList,
    ModelObject,
    PromptCreate,
    PromptRecord,
    PromptRender,
    ResponsesRequest,
)

router = APIRouter()


async def limited_identity(
    request: Request, identity: Annotated[str, Depends(authenticate)]
) -> AsyncIterator[str]:
    await request.app.state.rate_limiter.check(identity)
    await request.app.state.concurrency_limiter.acquire()
    try:
        yield identity
    finally:
        request.app.state.concurrency_limiter.release()


Identity = Annotated[str, Depends(limited_identity)]


@router.post("/v1/chat/completions")
async def chat_completions(payload: ChatCompletionRequest, request: Request, identity: Identity):
    body, prompt_meta = await request.app.state.gateway.prepare_body(
        "chat", payload.model_dump(exclude_none=True), payload.gateway_prompt
    )
    if payload.stream:
        iterator, request_id = await request.app.state.gateway.stream(
            "chat", body, identity, prompt_meta, request
        )
        return StreamingResponse(
            iterator,
            media_type="text/event-stream",
            headers={
                "X-Request-ID": request_id,
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
    result, request_id = await request.app.state.gateway.call_json("chat", body, identity, prompt_meta)
    return JSONResponse(result, headers={"X-Request-ID": request_id})


@router.post("/v1/responses")
async def responses(payload: ResponsesRequest, request: Request, identity: Identity):
    body, prompt_meta = await request.app.state.gateway.prepare_body(
        "responses", payload.model_dump(exclude_none=True), payload.gateway_prompt
    )
    if payload.stream:
        iterator, request_id = await request.app.state.gateway.stream(
            "responses", body, identity, prompt_meta, request
        )
        return StreamingResponse(
            iterator,
            media_type="text/event-stream",
            headers={
                "X-Request-ID": request_id,
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
    result, request_id = await request.app.state.gateway.call_json("responses", body, identity, prompt_meta)
    return JSONResponse(result, headers={"X-Request-ID": request_id})


@router.get("/v1/models", response_model=ModelList)
async def models(request: Request, _: Identity) -> ModelList:
    now = int(time.time())
    return ModelList(
        data=[ModelObject(id=name, created=now) for name in sorted(request.app.state.config.models)]
    )


@router.post("/v1/prompts", response_model=PromptRecord, status_code=201)
async def create_prompt(payload: PromptCreate, request: Request, _: Identity) -> PromptRecord:
    return await request.app.state.prompts.create_version(payload)


@router.get("/v1/prompts", response_model=list[PromptRecord])
async def list_prompts(request: Request, _: Identity) -> list[PromptRecord]:
    return await request.app.state.prompts.list()


@router.get("/v1/prompts/{prompt_id}", response_model=PromptRecord)
async def get_prompt(
    prompt_id: str, request: Request, _: Identity, version: int | None = None
) -> PromptRecord:
    return await request.app.state.prompts.get(prompt_id, version)


@router.post("/v1/prompts/{prompt_id}/versions/{version}/activate", response_model=PromptRecord)
async def activate_prompt(prompt_id: str, version: int, request: Request, _: Identity) -> PromptRecord:
    return await request.app.state.prompts.activate_version(prompt_id, version)


@router.post("/v1/prompts/{prompt_id}/render")
async def render_prompt(
    prompt_id: str, payload: PromptRender, request: Request, _: Identity
) -> dict[str, Any]:
    prompt, content = await request.app.state.prompts.render(prompt_id, payload.variables, payload.version)
    return {"id": prompt.id, "version": prompt.version, "role": prompt.role, "content": content}


@router.get("/admin/usage")
async def usage(request: Request, _: Identity, limit: int = Query(default=100, ge=1, le=1000)):
    return {"data": await request.app.state.usage.recent(limit)}


@router.get("/admin/routes")
async def route_status(request: Request, _: Identity):
    return {
        "models": {
            alias: model.model_dump(mode="json") for alias, model in request.app.state.config.models.items()
        },
        "circuits": request.app.state.model_router.status(),
    }


@router.get("/v1/streams/{request_id}/checkpoint")
async def stream_checkpoint(request_id: str, request: Request, _: Identity):
    checkpoint = await request.app.state.usage.get_checkpoint(request_id)
    if checkpoint is None:
        raise GatewayError(
            f"Stream checkpoint {request_id!r} was not found",
            status_code=404,
            error_type="invalid_request_error",
            code="checkpoint_not_found",
        )
    return checkpoint


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> dict[str, str]:
    return {"status": "ready", "service": request.app.state.config.service_name}
