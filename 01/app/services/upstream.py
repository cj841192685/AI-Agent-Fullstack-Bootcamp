from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import GatewayConfig, ProviderConfig
from app.core.errors import UpstreamError


@dataclass
class OpenStream:
    response: httpx.Response
    provider_name: str


class UpstreamClient:
    def __init__(self, config: GatewayConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self, provider: ProviderConfig, request_id: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
            **provider.extra_headers,
        }
        api_key = provider.api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _timeout(self, provider: ProviderConfig) -> httpx.Timeout:
        return httpx.Timeout(
            timeout=provider.timeout_seconds,
            connect=provider.connect_timeout_seconds,
        )

    async def request_json(
        self,
        provider_name: str,
        path: str,
        body: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        provider = self.config.providers[provider_name]
        url = f"{provider.base_url}{path}"
        try:
            response = await self.client.post(
                url,
                headers=self._headers(provider, request_id),
                json=body,
                timeout=self._timeout(provider),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamError(f"{provider_name}: {type(exc).__name__}", retryable=True) from exc
        if response.is_error:
            raise self._http_error(provider_name, response)
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise UpstreamError(f"{provider_name}: upstream returned invalid JSON") from exc

    async def open_stream(
        self,
        provider_name: str,
        path: str,
        body: dict[str, Any],
        request_id: str,
    ) -> OpenStream:
        provider = self.config.providers[provider_name]
        url = f"{provider.base_url}{path}"
        request = self.client.build_request(
            "POST",
            url,
            headers={**self._headers(provider, request_id), "Accept": "text/event-stream"},
            json=body,
            timeout=self._timeout(provider),
        )
        try:
            response = await self.client.send(request, stream=True)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamError(f"{provider_name}: {type(exc).__name__}", retryable=True) from exc
        if response.is_error:
            await response.aread()
            error = self._http_error(provider_name, response)
            await response.aclose()
            raise error
        return OpenStream(response=response, provider_name=provider_name)

    def _http_error(self, provider_name: str, response: httpx.Response) -> UpstreamError:
        retryable = response.status_code in self.config.retry.retry_statuses
        message = f"{provider_name}: HTTP {response.status_code}"
        details: Any = None
        try:
            details = response.json()
            upstream_message = details.get("error", {}).get("message") if isinstance(details, dict) else None
            if upstream_message:
                message = f"{message}: {upstream_message}"
        except (json.JSONDecodeError, AttributeError):
            details = response.text[:1000]
        status = response.status_code if 400 <= response.status_code < 500 else 502
        return UpstreamError(message, status_code=status, retryable=retryable, details=details)
