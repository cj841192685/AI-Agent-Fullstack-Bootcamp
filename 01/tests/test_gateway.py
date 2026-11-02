from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import gateway_client, make_config


@pytest.mark.asyncio
async def test_chat_retries_then_falls_back_and_records_usage(tmp_path, auth_headers):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.host, body["model"]))
        if request.url.host == "primary.test":
            return httpx.Response(429, json={"error": {"message": "busy"}})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_1",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    config = make_config(tmp_path / "gateway.db")
    async with gateway_client(config, handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello"
        assert response.headers["x-request-id"].startswith("req_")
        assert calls == [
            ("primary.test", "primary-model"),
            ("primary.test", "primary-model"),
            ("fallback.test", "fallback-model"),
        ]

        usage = (await client.get("/admin/usage", headers=auth_headers)).json()["data"][0]
        assert usage["provider"] == "fallback"
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 5
        assert usage["retries"] == 1
        assert usage["fallbacks"] == 1
        assert usage["cost_usd"] == pytest.approx(0.00005)


@pytest.mark.asyncio
async def test_structured_output_is_locally_validated_and_repaired(tmp_path, auth_headers):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = json.loads(request.content)
        content = '{"name": 123}' if call_count == 1 else '{"name": "Ada"}'
        if call_count == 2:
            assert "failed JSON Schema validation" in body["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl_{call_count}",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    config = make_config(
        tmp_path / "gateway.db",
        models={"smart": {"routes": [{"provider": "primary", "model": "primary-model", "api": "chat"}]}},
    )
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
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
        assert response.status_code == 200
        assert json.loads(response.json()["choices"][0]["message"]["content"]) == {"name": "Ada"}
        assert call_count == 2


@pytest.mark.asyncio
async def test_prompt_version_render_and_injection(tmp_path, auth_headers):
    upstream_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        upstream_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_prompt",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            },
        )

    config = make_config(tmp_path / "gateway.db")
    async with gateway_client(config, handler) as client:
        created = await client.post(
            "/v1/prompts",
            headers=auth_headers,
            json={
                "id": "reviewer",
                "name": "Reviewer",
                "content": "You review {{ language }} code.",
                "role": "system",
            },
        )
        assert created.status_code == 201
        assert created.json()["version"] == 1

        rendered = await client.post(
            "/v1/prompts/reviewer/render",
            headers=auth_headers,
            json={"variables": {"language": "Python"}},
        )
        assert rendered.json()["content"] == "You review Python code."

        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "smart",
                "messages": [{"role": "user", "content": "Review this"}],
                "gateway_prompt": {"id": "reviewer", "variables": {"language": "Python"}},
            },
        )
        assert response.status_code == 200
        assert upstream_bodies[-1]["messages"][0] == {
            "role": "system",
            "content": "You review Python code.",
        }


@pytest.mark.asyncio
async def test_streaming_sse_is_forwarded_and_usage_is_captured(tmp_path, auth_headers):
    def handler(_: httpx.Request) -> httpx.Response:
        sse = (
            'data: {"id":"chatcmpl_s","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl_s","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    config = make_config(
        tmp_path / "gateway.db",
        stream_checkpoint={"enabled": True, "flush_interval_seconds": 1, "max_chars": 10_000},
    )
    async with gateway_client(config, handler) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "smart", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as response:
            body = (await response.aread()).decode()
        assert response.status_code == 200
        assert '"content":"Hi"' in body
        assert "data: [DONE]" in body

        usage = (await client.get("/admin/usage", headers=auth_headers)).json()["data"][0]
        assert usage["input_tokens"] == 3
        assert usage["output_tokens"] == 1
        assert usage["first_token_ms"] is not None
        checkpoint = (
            await client.get(
                f"/v1/streams/{response.headers['x-request-id']}/checkpoint", headers=auth_headers
            )
        ).json()
        assert checkpoint["status"] == "success"
        assert checkpoint["content"] == "Hi"


@pytest.mark.asyncio
async def test_responses_api_and_auth_error(tmp_path, auth_headers):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["model"] == "primary-model"
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "output_text": "done",
                "output": [],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    config = make_config(tmp_path / "gateway.db")
    async with gateway_client(config, handler) as client:
        unauthorized = await client.get("/v1/models")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["type"] == "authentication_error"

        response = await client.post(
            "/v1/responses",
            headers=auth_headers,
            json={"model": "smart", "input": "hello"},
        )
        assert response.status_code == 200
        assert response.json()["output_text"] == "done"


@pytest.mark.asyncio
async def test_gateway_rate_limit_uses_openai_error_shape(tmp_path, auth_headers):
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Model listing must not call an upstream provider")

    config = make_config(
        tmp_path / "gateway.db",
        rate_limit={"enabled": True, "requests_per_minute": 1, "burst": 1},
    )
    async with gateway_client(config, handler) as client:
        first = await client.get("/v1/models", headers=auth_headers)
        second = await client.get("/v1/models", headers=auth_headers)
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["error"]["type"] == "rate_limit_error"
        assert second.json()["error"]["code"] == "rate_limit_exceeded"
