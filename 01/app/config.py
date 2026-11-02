from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?}")


def _expand_env(value: object) -> object:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            return ""

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


class RetryConfig(BaseModel):
    max_attempts_per_route: int = Field(default=2, ge=1, le=5)
    base_delay_seconds: float = Field(default=0.25, ge=0, le=10)
    max_delay_seconds: float = Field(default=4, ge=0, le=60)
    retry_statuses: set[int] = {408, 409, 429, 500, 502, 503, 504}


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = Field(default=5, ge=1)
    cooldown_seconds: float = Field(default=30, ge=0)


class RateLimitConfig(BaseModel):
    enabled: bool = True
    requests_per_minute: int = Field(default=60, ge=1)
    burst: int = Field(default=10, ge=1)


class ConcurrencyConfig(BaseModel):
    max_concurrent_requests: int = Field(default=20, ge=1, le=10_000)


class StreamCheckpointConfig(BaseModel):
    enabled: bool = False
    flush_interval_seconds: float = Field(default=1, ge=0.1, le=60)
    max_chars: int = Field(default=200_000, ge=1_000, le=10_000_000)


class ProviderConfig(BaseModel):
    base_url: str
    api_key: SecretStr = SecretStr("")
    enabled: bool = True
    timeout_seconds: float = Field(default=120, gt=0)
    connect_timeout_seconds: float = Field(default=10, gt=0)
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_url(self) -> ProviderConfig:
        self.base_url = self.base_url.rstrip("/")
        return self


class RouteTarget(BaseModel):
    provider: str
    model: str
    weight: int = Field(default=1, ge=1)
    api: Literal["chat", "responses", "both"] = "both"


class ModelRouteConfig(BaseModel):
    strategy: Literal["priority", "weighted_round_robin"] = "priority"
    routes: list[RouteTarget] = Field(min_length=1)


class PriceConfig(BaseModel):
    input_per_million: float = Field(default=0, ge=0)
    output_per_million: float = Field(default=0, ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)


class GatewayConfig(BaseModel):
    service_name: str = "llm-gateway"
    api_keys: list[SecretStr] = Field(default_factory=list)
    database_url: str = "data/gateway.db"
    structured_output_retries: int = Field(default=1, ge=0, le=3)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    stream_checkpoint: StreamCheckpointConfig = Field(default_factory=StreamCheckpointConfig)
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelRouteConfig]
    pricing: dict[str, PriceConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_routes(self) -> GatewayConfig:
        unknown = {
            route.provider
            for model in self.models.values()
            for route in model.routes
            if route.provider not in self.providers
        }
        if unknown:
            raise ValueError(f"Routes reference unknown providers: {sorted(unknown)}")
        return self


def load_config(path: str | Path | None = None) -> GatewayConfig:
    config_path = Path(path or os.getenv("GATEWAY_CONFIG", "gateway.yaml"))
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    if not config_path.exists() and path is None and "GATEWAY_CONFIG" not in os.environ:
        bundled_example = Path(__file__).resolve().parent.parent / "gateway.example.yaml"
        if bundled_example.exists():
            config_path = bundled_example
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = GatewayConfig.model_validate(_expand_env(raw))
    db_path = Path(config.database_url)
    if not db_path.is_absolute():
        config.database_url = str((config_path.parent / db_path).resolve())
    return config
