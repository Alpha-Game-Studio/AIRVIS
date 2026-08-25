"""The one canonical tool registry."""

from __future__ import annotations

import asyncio
import builtins
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from ..core.asyncutil import run_blocking
from ..core.errors import DuplicateRegistrationError, PermissionDeniedError, ToolExecutionError, ToolTimeoutError, UnknownToolError
from ..core.events import EventBus, EventType
from ..security.permissions import ApprovalHandler, PermissionManager
from .base import RiskLevel, Tool, ToolContext, ToolResult


class ToolRegistry:
    """Holds tools and executes them through the security pipeline.

    Provider APIs may require function names containing only ``[A-Za-z0-9_-]``.
    AIRVIS keeps its namespaced names (for example ``filesystem.read``) as the
    canonical identity and accepts the provider-safe alias at the execution
    boundary as well.
    """

    def __init__(self, workspace: Path | str | None = None, *, permissions: PermissionManager | None = None,
                 event_bus: EventBus | None = None, install_builtins: bool = True) -> None:
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.event_bus = event_bus
        self.permissions = permissions or PermissionManager(workspace=self.workspace, event_bus=event_bus)
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, str] = {}
        if install_builtins:
            from .builtin import builtin_tools
            for tool in builtin_tools():
                self.register(tool)

    def register(self, tool: Tool, *, replace: bool = True) -> Tool:
        if not isinstance(tool, Tool):
            raise TypeError(f"expected a Tool instance, got {type(tool).__name__}")
        if tool.name in self._tools and not replace:
            raise DuplicateRegistrationError(f"tool already registered: {tool.name}", tool=tool.name)
        self._tools[tool.name] = tool
        self._aliases[_provider_safe_name(tool.name)] = tool.name
        return tool

    def register_all(self, tools: Iterable[Tool], *, replace: bool = True) -> None:
        for tool in tools:
            self.register(tool, replace=replace)

    def unregister(self, name: str) -> bool:
        canonical = self._canonical(name)
        removed = self._tools.pop(canonical, None) is not None
        self._aliases.pop(_provider_safe_name(canonical), None)
        return removed

    def _canonical(self, name: str) -> str:
        token = str(name)
        return token if token in self._tools else self._aliases.get(token, token)

    def get(self, name: str) -> Tool:
        canonical = self._canonical(name)
        try:
            return self._tools[canonical]
        except KeyError as exc:
            raise UnknownToolError(f"unknown tool: {name}", tool=name) from exc

    def has(self, name: str) -> bool:
        return self._canonical(name) in self._tools

    def names(self) -> builtins.list[str]:
        return sorted(self._tools)

    def list(self) -> builtins.list[dict[str, Any]]:
        return [self._describe(tool) for tool in sorted(self._tools.values(), key=lambda item: item.name)]

    def matching(self, allowed: Iterable[str] | None = None) -> builtins.list[Tool]:
        if allowed is None:
            return list(self._tools.values())
        allowed_set = {self._canonical(name) for name in allowed}
        return [tool for name, tool in self._tools.items() if name in allowed_set]

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)

    def _describe(self, tool: Tool) -> dict[str, Any]:
        schema = tool.schema()
        schema["effective_risk"] = self.permissions.effective_risk(tool).name
        return schema

    def context(self, *, workflow_id: str | None = None, task_id: str | None = None,
                agent_id: str | None = None, timeout: float = 60.0) -> ToolContext:
        return ToolContext(workspace=self.workspace, permissions=self.permissions, workflow_id=workflow_id,
                           task_id=task_id, agent_id=agent_id, timeout=timeout,
                           allow_network=self.permissions.config.allow_network)

    async def call(self, name: str, arguments: dict[str, Any] | None = None, *, context: ToolContext | None = None,
                   confirm: bool = False, agent_permissions: set[str] | frozenset[str] | None = None,
                   agent_tools: set[str] | frozenset[str] | None = None, approval_handler: ApprovalHandler | None = None,
                   timeout: float | None = None) -> ToolResult:
        arguments = dict(arguments or {})
        canonical = self._canonical(name)
        tool = self.get(canonical)
        ctx = context or self.context()
        effective_timeout = timeout if timeout is not None else ctx.timeout
        self._emit(EventType.TOOL_STARTED, canonical, ctx, status="started")
        started = time.perf_counter()
        try:
            tool.validate_arguments(arguments)
            await self.permissions.authorize(tool, arguments, agent_permissions=agent_permissions,
                                             agent_tools={self._canonical(item) for item in agent_tools} if agent_tools else agent_tools,
                                             confirm=confirm, approval_handler=approval_handler,
                                             workflow_id=ctx.workflow_id, task_id=ctx.task_id, agent_id=ctx.agent_id)
        except PermissionDeniedError:
            self._emit(EventType.TOOL_FAILED, canonical, ctx, status="denied', duration_ms=(time.perf_counter() - started) * 1000)
            raise
        try:
            output = await asyncio.wait_for(tool.run(ctx, **arguments), timeout=effective_timeout)
        except asyncio.TimeoutError as exc:
            duration = (time.perf_counter() - started) * 1000
            self._emit(EventType.TOOL_FAILED, canonical, ctx, status="timeout", duration_ms=duration)
            raise ToolTimeoutError(f"{canonical} timed out after {effective_timeout}s", tool=canonical, timeout=effective_timeout) from exc
        except (PermissionDeniedError, ToolExecutionError):
            self._emit(EventType.TOOL_FAILED, canonical, ctx, status="failed', duration_ms=(time.perf_counter() - started) * 1000)
            raise
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            self._emit(EventType.TOOL_FAILED, canonical, ctx, status="failed", duration_ms=duration)
            raise ToolExecutionError(f"{canonical} failed: {exc}", tool=canonical, cause=type(exc).__name__) from exc
        duration = (time.perf_counter() - started) * 1000
        result = _as_result(canonical, output, duration)
        self._emit(EventType.TOOL_COMPLETED, canonical, ctx, status="ok", duration_ms=duration)
        return result

    def execute(self, name: str, arguments: dict[str, Any] | None = None, confirm: bool = False, *,
                context: ToolContext | None = None, timeout: float | None = None) -> Any:
        return run_blocking(self.call(name, arguments, context=context, confirm=confirm, timeout=timeout)).unwrap()

    def _emit(self, event_type: EventType, tool: str, ctx: ToolContext, *, status: str,
              duration_ms: float | None = None) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(event_type, tool=tool, workflow_id=ctx.workflow_id, task_id=ctx.task_id,
                               agent_id=ctx.agent_id, status=status, duration_ms=duration_ms)


def _provider_safe_name(name: str) -> str:
    """Mirror the provider-side OpenAI naming restriction."""
    import re
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(name))
    safe = re.sub(r"_+", "_", safe).strip("_") or "tool"
    return safe[:64]


def _as_result(name: str, output: Any, duration_ms: float) -> ToolResult:
    if isinstance(output, ToolResult):
        output.duration_ms = output.duration_ms or duration_ms
        output.tool = output.tool or name
        return output
    return ToolResult(tool=name, ok=True, output=output, duration_ms=duration_ms)


def command_risk(command: str) -> str:
    from .terminal import classify_command
    return classify_command(command).name


__all__ = ["RiskLevel", "ToolRegistry", "command_risk"]
