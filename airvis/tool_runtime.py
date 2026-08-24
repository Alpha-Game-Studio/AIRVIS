"""First-class tool runtime for autonomous AIRVIS agents."""
from __future__ import annotations

import asyncio
import inspect
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., Any]
    capabilities: tuple[str, ...] = ()
    risk: str = "LOW"


@dataclass
class ToolResult:
    tool: str
    ok: bool
    output: Any = None
    error: str | None = None


class ToolRuntime:
    """Controlled tool execution layer; model selection never bypasses policy."""

    def __init__(self, workspace: str | Path, *, allow_network: bool = True) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.allow_network = allow_network
        self.tools: dict[str, ToolSpec] = {}
        self.register(ToolSpec("filesystem.read", "Read a UTF-8 file inside the workspace.", self.read, ("filesystem", "read")))
        self.register(ToolSpec("filesystem.write", "Write a UTF-8 file inside the workspace.", self.write, ("filesystem", "write"), "MEDIUM"))
        self.register(ToolSpec("filesystem.list", "List entries inside the workspace.", self.list_files, ("filesystem", "inspect")))
        self.register(ToolSpec("shell.exec", "Execute a shell command in the workspace.", self.shell, ("terminal", "shell"), "HIGH"))

    def register(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def _safe_path(self, path: str) -> Path:
        candidate = (self.workspace / path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise PermissionError("path escapes AIRVIS workspace")
        return candidate

    def read(self, path: str) -> str:
        return self._safe_path(path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> str:
        target = self._safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target.relative_to(self.workspace))

    def list_files(self, path: str = ".") -> list[str]:
        return [str(p.relative_to(self.workspace)) for p in self._safe_path(path).iterdir()]

    def shell(self, command: str, timeout: float = 60.0) -> str:
        result = subprocess.run(command, shell=True, cwd=self.workspace, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"command exited {result.returncode}")
        return result.stdout

    async def execute(self, name: str, arguments: dict[str, Any] | None = None, *, approved: bool = False) -> ToolResult:
        spec = self.tools.get(name)
        if not spec:
            return ToolResult(name, False, error=f"unknown tool: {name}")
        if spec.risk in {"HIGH", "CRITICAL"} and not approved:
            return ToolResult(name, False, error=f"approval required for {spec.risk} tool")
        try:
            value = spec.handler(**(arguments or {}))
            if inspect.isawaitable(value):
                value = await value
            return ToolResult(name, True, output=value)
        except Exception as exc:
            return ToolResult(name, False, error=str(exc))

    def describe(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "description": s.description, "capabilities": s.capabilities, "risk": s.risk} for s in self.tools.values()]


__all__ = ["ToolRuntime", "ToolSpec", "ToolResult"]
