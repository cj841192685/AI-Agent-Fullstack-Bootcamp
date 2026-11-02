from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChatMessage(FlexibleModel):
    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = None


class PromptReference(FlexibleModel):
    id: str
    version: int | None = Field(default=None, ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    position: Literal["prepend", "append"] = "prepend"


class ChatCompletionRequest(FlexibleModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    response_format: dict[str, Any] | None = None
    gateway_prompt: PromptReference | None = None


class ResponsesRequest(FlexibleModel):
    model: str
    input: Any
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    text: dict[str, Any] | None = None
    gateway_prompt: PromptReference | None = None


class PromptCreate(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    description: str | None = None
    role: Literal["system", "developer"] = "system"
    activate: bool = True


class PromptRender(BaseModel):
    version: int | None = Field(default=None, ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)


class PromptRecord(BaseModel):
    id: str
    version: int
    name: str
    description: str | None
    role: str
    content: str
    is_active: bool
    created_at: str


class ModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "llm-gateway"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelObject]


class ErrorDetail(BaseModel):
    message: str
    type: str
    param: str | None = None
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
