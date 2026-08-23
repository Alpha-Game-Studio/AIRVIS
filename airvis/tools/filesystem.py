"""Workspace-scoped filesystem tools."""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.errors import ToolExecutionError
from .base import RiskLevel, Tool, ToolContext, ToolResult

MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000
MAX_SEARCH_RESULTS = 500


class FilesystemReadTool(Tool):
    name = "filesystem.read"
    description = "Read a UTF-8 text file inside the workspace."
    risk = RiskLevel.SAFE
    tags = frozenset({"filesystem", "read"})
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative path"},
            "max_bytes": {"type": "integer", "description": "Truncate after this many bytes"},
        },
        "required": ["path"],
    }

    async def run(self, context: ToolContext, path: str, max_bytes: int = MAX_READ_BYTES) -> ToolResult:
        target = context.resolve_path(path)
        if not target.is_file():
            raise ToolExecutionError(f"not a file: {path}", tool=self.name, path=path)
        limit = max(1, min(int(max_bytes), MAX_READ_BYTES))
        data = await asyncio.to_thread(target.read_bytes)
        truncated = len(data) > limit
        text = data[:limit].decode("utf-8", errors="replace")
        return ToolResult(
            tool=self.name,
            output=text,
            metadata={"path": str(target), "bytes": len(data), "truncated": truncated},
        )


class FilesystemWriteTool(Tool):
    name = "filesystem.write"
    description = "Create or overwrite a UTF-8 text file inside the workspace."
    risk = RiskLevel.MEDIUM
    required_permissions = frozenset({"filesystem.write"})
    tags = frozenset({"filesystem", "write"})
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean"},
        },
        "required": ["path", "content"],
    }

    async def run(self, context: ToolContext, path: str, content: str, append: bool = False) -> ToolResult:
        target = context.resolve_path(path)
        payload = str(content)
        if len(payload.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolExecutionError("file content exceeds 1 MB", tool=self.name, path=path)
        existed = target.is_file()

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a" if append else "w", encoding="utf-8") as stream:
                stream.write(payload)

        await asyncio.to_thread(_write)
        relative = _relative(context, target)
        return ToolResult(
            tool=self.name,
            output=relative,
            metadata={"path": str(target), "created": not existed, "appended": bool(append)},
            artifacts=[{"type": "file", "name": relative, "path": str(target)}],
        )


class FilesystemDeleteTool(Tool):
    name = "filesystem.delete"
    description = "Delete an existing file inside the workspace."
    risk = RiskLevel.HIGH
    required_permissions = frozenset({"filesystem.write"})
    tags = frozenset({"filesystem", "destructive"})
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def run(self, context: ToolContext, path: str) -> ToolResult:
        target = context.resolve_path(path)
        if target == context.workspace or not target.is_file():
            raise ToolExecutionError("only existing files can be deleted", tool=self.name, path=path)
        await asyncio.to_thread(target.unlink)
        return ToolResult(tool=self.name, output=_relative(context, target), metadata={"path": str(target)})


class FilesystemSearchTool(Tool):
    name = "filesystem.search"
    description = "List workspace files matching a glob pattern."
    risk = RiskLevel.SAFE
    tags = frozenset({"filesystem", "read"})
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
            "limit": {"type": "integer"},
        },
        "required": [],
    }

    async def run(self, context: ToolContext, pattern: str = "*", limit: int = MAX_SEARCH_RESULTS) -> list[str]:
        root = context.workspace
        capped = max(1, min(int(limit), MAX_SEARCH_RESULTS))

        def _search() -> list[str]:
            found: list[str] = []
            for candidate in root.rglob(pattern):
                if not candidate.is_file() or _is_ignored(candidate):
                    continue
                found.append(str(candidate.relative_to(root)))
                if len(found) >= capped:
                    break
            return sorted(found)

        return await asyncio.to_thread(_search)


class FilesystemGrepTool(Tool):
    name = "filesystem.grep"
    description = "Search file contents for a literal substring inside the workspace."
    risk = RiskLevel.SAFE
    tags = frozenset({"filesystem", "read"})
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "pattern": {"type": "string", "description": "Glob restricting which files are searched"},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    }

    async def run(
        self, context: ToolContext, query: str, pattern: str = "**/*", limit: int = 100
    ) -> list[dict[str, Any]]:
        root = context.workspace
        needle = str(query)
        capped = max(1, min(int(limit), 500))

        def _grep() -> list[dict[str, Any]]:
            hits: list[dict[str, Any]] = []
            for candidate in root.rglob(pattern):
                if not candidate.is_file() or _is_ignored(candidate):
                    continue
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for number, line in enumerate(text.splitlines(), start=1):
                    if needle in line:
                        hits.append(
                            {"path": str(candidate.relative_to(root)), "line": number, "text": line.strip()[:400]}
                        )
                        if len(hits) >= capped:
                            return hits
            return hits

        return await asyncio.to_thread(_grep)


class SystemInfoTool(Tool):
    name = "system.info"
    description = "Return basic interpreter and workspace information."
    risk = RiskLevel.SAFE
    tags = frozenset({"system", "read"})
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, context: ToolContext) -> dict[str, str]:
        import platform
        import sys

        return {
            "platform": platform.system(),
            "release": platform.release(),
            "python": sys.version.split()[0],
            "workspace": str(context.workspace),
        }


_IGNORED_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


def _is_ignored(path: Any) -> bool:
    return any(part in _IGNORED_PARTS for part in path.parts)


def _relative(context: ToolContext, target: Any) -> str:
    try:
        return str(target.relative_to(context.workspace))
    except ValueError:  # pragma: no cover - outside workspace paths are rejected earlier
        return str(target)


def filesystem_tools() -> list[Tool]:
    return [
        FilesystemReadTool(),
        FilesystemWriteTool(),
        FilesystemDeleteTool(),
        FilesystemSearchTool(),
        FilesystemGrepTool(),
        SystemInfoTool(),
    ]


__all__ = [
    "FilesystemDeleteTool",
    "FilesystemGrepTool",
    "FilesystemReadTool",
    "FilesystemSearchTool",
    "FilesystemWriteTool",
    "SystemInfoTool",
    "filesystem_tools",
]
