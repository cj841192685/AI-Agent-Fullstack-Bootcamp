from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core.rate_limit import InMemoryConcurrencyLimiter
from app.services.gateway import GatewayService
from app.services.router import ModelRouter
from app.services.upstream import UpstreamClient
from app.services.usage import UsageRepository
from tests.conftest import gateway_client, make_config


def chat_response(content: str = "ok", *, model: str = "primary-model") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        },
    )


@pytest.mark.asyncio
async def test_missing_price_is_null_not_fake_zero(tmp_path, auth_headers):
    config = make_config(tmp_path / "gateway.db", pricing={})

    async with gateway_client(config, lambda _: chat_response()) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        usage = (await client.get("/admin/usage", headers=auth_headers)).json()["data"][0]
        assert usage["cost_usd"] is None


@pytest.mark.asyncio
async def test_invalid_prompt_and_model_never_reach_upstream(tmp_path, auth_headers):
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return chat_response()

    config = make_config(tmp_path / "gateway.db")
    async with gateway_client(config, handler) as client:
        created = await client.post(
            "/v1/prompts",
            headers=auth_headers,
            json={"id": "strict", "name": "Strict", "content": "Hello {{ name }}"},
        )
        assert created.status_code == 201

        missing_variable = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "smart",
                "messages": [{"role": "user", "content": "hi"}],
                "gateway_prompt": {"id": "strict", "variables": {}},
            },
        )
        unknown_model = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "does-not-exist", "messages": [{"role": "user", "content": "hi"}]},
        )
        unknown_prompt = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "smart",
                "messages": [{"role": "user", "content": "hi"}],
                "gateway_prompt": {"id": "missing", "variables": {}},
            },
        )

        assert missing_variable.status_code == 422
        assert missing_variable.json()["error"]["code"] == "prompt_render_error"
        assert unknown_model.status_code == 404
        assert unknown_model.json()["error"]["code"] == "model_not_found"
        assert unknown_prompt.status_code == 404
        assert unknown_prompt.json()["error"]["code"] == "prompt_not_found"
        assert calls == 0


@pytest.mark.asyncio
async def test_prompt_version_can_be_rolled_back(tmp_path, auth_headers):
    config = make_config(tmp_path / "gateway.db")
    async with gateway_client(config, lambda _: chat_response()) as client:
        for version in (1, 2):
            response = await client.post(
                "/v1/prompts",
                headers=auth_headers,
                json={"id": "reviewer", "name": "Reviewer", "content": f"Version {version}"},
            )
            assert response.status_code == 201

        activated = await client.post(
            "/v1/prompts/reviewer/versions/1/activate", headers=auth_headers
        )
        current = await client.get("/v1/prompts/reviewer", headers=auth_headers)
        old = await client.get("/v1/prompts/reviewer", headers=auth_headers)
        assert activated.json()["is_active"] is True
        assert current.json()["version"] == 1
        assert current.json()["content"] == "Version 1"
        assert old.json()["version"] == 1


@pytest.mark.asyncio
async def test_models_expose_aliases_and_x_api_key_is_hashed(tmp_path):
    config = make_config(tmp_path / "gateway.db")
    async with gateway_client(config, lambda _: chat_response()) as client:
        response = await client.get("/v1/models", headers={"X-API-Key": "test-key"})
        assert response.status_code == 200
        model = response.json()["data"][0]
        assert model == {
            "id": "smart",
            "object": "model",
            "created": model["created"],
            "owned_by": "llm-gateway",
        }
        assert len(response.json()["data"]) == 1
        assert "primary-model" not in response.text
        await client.post(
            "/v1/chat/completions",
            headers={"X-API-Key": "test-key"},
            json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]},
        )
        usage = (await client.get("/admin/usage", headers={"X-API-Key": "test-key"})).json()["data"][0]
        assert usage["api_key_hash"] != "test-key"
        assert len(usage["api_key_hash"]) == 16


@pytest.mark.asyncio
async def test_responses_route_filters_incompatible_provider(tmp_path, auth_headers):
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(
            200,
            json={"id": "resp_1", "output_text": "done", "output": [], "usage": {}},
        )

    config = make_config(
        tmp_path / "gateway.db",
        models={
            "smart": {
                "routes": [
                    {"provider": "primary", "model": "primary-model", "api": "chat"},
                    {"provider": "fallback", "model": "fallback-model", "api": "responses"},
                ]
            }
        },
    )
    async with gateway_client(config, handler) as client:
        response = await client.post(
            "/v1/responses", headers=auth_headers, json={"model": "smart", "input": "hello"}
        )
        assert response.status_code == 200
        assert hosts == ["fallback.test"]


@pytest.mark.asyncio
async def test_structured_output_failure_is_bounded(tmp_path, auth_headers):
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return chat_response('{"name": 123}')

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    config = make_config(tmp_path / "gateway.db")
    async with gateway_client(config, handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "smart",
                "messages": [{"role": "user", "content": "Name"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "person", "strict": True, "schema": schema},
                },
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_model_output"
        assert calls == 2
        usage = (await client.get("/admin/usage", headers=auth_headers)).json()["data"][0]
        assert usage["status"] == "error"
        assert usage["error_type"] == "structured_output_error"


class FailingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield (
            b'data: {"id":"chatcmpl_s","choices":[{"delta":{"content":"partial"}}]}\n\n'
        )
        raise httpx.ReadError("connection lost")


@pytest.mark.asyncio
async def test_stream_never_falls_back_after_first_chunk(tmp_path, auth_headers):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=FailingStream()
            )
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    config = make_config(tmp_path / "gateway.db")
    async with gateway_client(config, handler) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "smart", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as response:
            body = (await response.aread()).decode()
        assert '"content":"partial"' in body
        assert "stream_error" in body
        assert "data: [DONE]" in body
        assert calls == ["primary.test"]
        usage = (await client.get("/admin/usage", headers=auth_headers)).json()["data"][0]
        assert usage["status"] == "error"
        assert usage["fallbacks"] == 0


class OneChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"cancel me"}}]}\n\n'


@pytest.mark.asyncio
async def test_disconnect_closes_upstream_and_records_cancellation(tmp_path):
    config = make_config(tmp_path / "gateway.db")
    usage = UsageRepository(str(tmp_path / "gateway.db"), config)
    await usage.initialize()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=OneChunkStream()
            )
        )
    )
    upstream = UpstreamClient(config, upstream_client)
    service = GatewayService(config, ModelRouter(config), upstream, SimpleNamespace(), usage)
    client_request = SimpleNamespace(is_disconnected=AsyncTrue())

    iterator, request_id = await service.stream(
        "chat",
        {"model": "smart", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        "key-hash",
        (None, None),
        client_request,
    )
    assert [chunk async for chunk in iterator] == []
    event = (await usage.recent(1))[0]
    assert event["request_id"] == request_id
    assert event["status"] == "cancelled"
    assert event["status_code"] == 499
    await upstream_client.aclose()


class AsyncTrue:
    async def __call__(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_concurrency_limiter_blocks_excess_work():
    limiter = InMemoryConcurrencyLimiter(1)
    await limiter.acquire()
    waiting = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    assert not waiting.done()
    limiter.release()
    await asyncio.wait_for(waiting, timeout=1)
    limiter.release()


@pytest.mark.asyncio
async def test_ttft_parser_ignores_empty_role_delta():
    event: Any = SimpleNamespace(input_tokens=0, output_tokens=0, cached_tokens=0)
    buffer = (
        'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    )
    remaining, delta = GatewayService._collect_stream_data(buffer, event)
    assert remaining == ""
    assert delta == "Hello"
