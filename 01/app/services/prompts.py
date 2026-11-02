from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from app.core.errors import GatewayError
from app.schemas import PromptCreate, PromptRecord


class PromptRepository:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self.env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS prompts (
                    id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (id, version)
                )
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_prompts_active ON prompts(id, is_active)")
            await db.commit()

    async def create_version(self, prompt: PromptCreate) -> PromptRecord:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM prompts WHERE id = ?", (prompt.id,)
            )
            version = int((await cursor.fetchone())[0])
            if prompt.activate:
                await db.execute("UPDATE prompts SET is_active = 0 WHERE id = ?", (prompt.id,))
            await db.execute(
                "INSERT INTO prompts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prompt.id,
                    version,
                    prompt.name,
                    prompt.description,
                    prompt.role,
                    prompt.content,
                    int(prompt.activate),
                    now,
                ),
            )
            await db.commit()
        return PromptRecord(
            id=prompt.id,
            version=version,
            name=prompt.name,
            description=prompt.description,
            role=prompt.role,
            content=prompt.content,
            is_active=prompt.activate,
            created_at=now,
        )

    async def activate_version(self, prompt_id: str, version: int) -> PromptRecord:
        await self.get(prompt_id, version)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("UPDATE prompts SET is_active = 0 WHERE id = ?", (prompt_id,))
            await db.execute(
                "UPDATE prompts SET is_active = 1 WHERE id = ? AND version = ?",
                (prompt_id, version),
            )
            await db.commit()
        return await self.get(prompt_id, version)

    async def get(self, prompt_id: str, version: int | None = None) -> PromptRecord:
        query = (
            "SELECT id, version, name, description, role, content, is_active, created_at "
            "FROM prompts WHERE id = ?"
        )
        params: tuple[Any, ...] = (prompt_id,)
        if version is None:
            query += " AND is_active = 1 ORDER BY version DESC LIMIT 1"
        else:
            query += " AND version = ?"
            params = (prompt_id, version)
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            row = await cursor.fetchone()
        if row is None:
            raise GatewayError(
                f"Prompt {prompt_id!r} version {version or 'active'} was not found",
                status_code=404,
                error_type="invalid_request_error",
                code="prompt_not_found",
            )
        return PromptRecord(**dict(row))

    async def list(self) -> list[PromptRecord]:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, version, name, description, role, content, is_active, created_at "
                "FROM prompts ORDER BY id, version DESC"
            )
            rows = await cursor.fetchall()
        return [PromptRecord(**dict(row)) for row in rows]

    async def render(
        self, prompt_id: str, variables: dict[str, Any], version: int | None = None
    ) -> tuple[PromptRecord, str]:
        prompt = await self.get(prompt_id, version)
        try:
            rendered = self.env.from_string(prompt.content).render(**variables)
        except Exception as exc:
            raise GatewayError(
                f"Prompt rendering failed: {exc}",
                status_code=422,
                error_type="invalid_request_error",
                code="prompt_render_error",
            ) from exc
        return prompt, rendered
