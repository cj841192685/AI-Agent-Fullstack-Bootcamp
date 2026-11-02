from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import ValidationError, validate

from app.core.errors import StructuredOutputError

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def schema_from_request(api: str, body: dict[str, Any]) -> dict[str, Any] | None:
    if api == "chat":
        response_format = body.get("response_format") or {}
        if response_format.get("type") != "json_schema":
            return None
        return (response_format.get("json_schema") or {}).get("schema")
    text_format = (body.get("text") or {}).get("format") or {}
    if text_format.get("type") != "json_schema":
        return None
    return text_format.get("schema")


def content_from_response(api: str, payload: dict[str, Any]) -> str:
    try:
        if api == "chat":
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, str):
                return content
        else:
            if isinstance(payload.get("output_text"), str):
                return payload["output_text"]
            for output in payload.get("output", []):
                for item in output.get("content", []):
                    if item.get("type") in {"output_text", "text"} and isinstance(item.get("text"), str):
                        return item["text"]
    except (KeyError, IndexError, TypeError):
        pass
    raise StructuredOutputError("Could not find textual model output to validate")


def validate_response(api: str, payload: dict[str, Any], schema: dict[str, Any]) -> Any:
    content = content_from_response(api, payload)
    fenced = _FENCE.match(content)
    if fenced:
        content = fenced.group(1)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            "Model output is not valid JSON",
            details={"line": exc.lineno, "column": exc.colno, "message": exc.msg},
        ) from exc
    try:
        validate(instance=parsed, schema=schema)
    except ValidationError as exc:
        raise StructuredOutputError(
            "Model output does not match the requested JSON Schema",
            details={"path": list(exc.absolute_path), "message": exc.message},
        ) from exc
    return parsed


def repair_instruction(error: StructuredOutputError, schema: dict[str, Any]) -> str:
    return (
        "Your previous response failed JSON Schema validation. Return only corrected JSON, "
        "with no Markdown fences or explanation.\n"
        f"Validation error: {error.message}; details={json.dumps(error.details, ensure_ascii=False)}\n"
        f"Required schema: {json.dumps(schema, ensure_ascii=False)}"
    )
