"""The in-process execution backend.

Runs an agent by (1) executing the deterministic tool steps the planner
attached, (2) asking the provider to reason over the resulting observations and
(3) executing any further tool calls the model asks for — all through the one
canonical tool registry and its permission gate.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from ..core.errors import (
    ApprovalRequiredError,
    BackendError,
    PermissionDeniedError,
    ProviderError,
    ToolError,
)
from ..core.health import HealthState, HealthStatus
from ..providers.base import GenerationRequest, Message, ToolCall
from ..providers.registry import ProviderRegistry
from ..tools.registry import ToolRegistry
from .base import Backend, BackendType, ExecutionRequest, ExecutionResult

MAX_OBSERVATION_CHARS = 6000


class NativeBackend(Backend):
    """Executes agents inside the AIRVIS process."""

    id = "native"
    type = BackendType.NATIVE
    capabilities = frozenset({"chat", "tools", "streaming", "cancel", "sessions", "context"})
    description = "In-process agent runtime with direct tool access."

    def __init__(
        self,
        providers: ProviderRegistry,
        tools: ToolRegistry,
        *,
        id: str = "native",
        tool_filter: frozenset[str] | None = None,
        max_tool_calls: int = 20,
        **overrides: Any,
    ) -> None:
        self.providers = providers
        self.tools = tools
        self.id = id
        self.tool_filter = tool_filter
        self.max_tool_calls = max_tool_calls
        self._running: dict[str, asyncio.Event] = {}
        super().__init__(**overrides)

    # -- execution -------------------------------------------------------------

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.perf_counter()
        cancel_event = asyncio.Event()
        self._running[request.execution_id] = cancel_event
        result = ExecutionResult(backend_id=self.id, execution_id=request.execution_id)
        tool_results: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        observations: list[str] = []
        calls_made = 0

        try:
            allowed = self._allowed_tools(request)

            # 1. deterministic steps chosen by the planner
            for step in request.tool_plan:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                if calls_made >= self.max_tool_calls:
                    break
                calls_made += 1
                record = await self._invoke(request, step.tool, step.arguments, allowed, optional=step.optional)
                tool_results.append(record)
                artifacts.extend(record.pop("_artifacts", []))
                observations.append(_render_observation(record))

            # 2. provider reasoning, optionally driving further tool calls
            messages = self._messages(request, observations)
            iterations = 0
            text = ""
            usage: dict[str, int] = {}
            provider_id = request.provider_id or request.agent.provider_id
            model = request.model or request.agent.model or ""

            while iterations < max(1, request.max_iterations):
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                iterations += 1
                generation = await self.providers.generate(
                    GenerationRequest(
                        messages=messages,
                        model=model,
                        tools=[tool.schema() for tool in self.tools.matching(allowed)],
                        temperature=request.temperature,
                        timeout=request.timeout,
                        metadata={"task_id": request.task_id, "agent_id": request.agent.id},
                    ),
                    provider_id=provider_id,
                    workflow_id=request.workflow_id,
                    task_id=request.task_id,
                    agent_id=request.agent.id,
                )
                text = generation.text
                provider_id = generation.provider or provider_id
                model = generation.model or model
                usage = generation.usage.to_dict()

                calls = generation.tool_calls or _parse_inline_tool_call(text)
                if not calls or calls_made >= self.max_tool_calls:
                    break

                messages.append(Message("assistant", text or "[tool call]"))
                for call in calls:
                    if calls_made >= self.max_tool_calls:
                        break
                    calls_made += 1
                    record = await self._invoke(request, call.name, call.arguments, allowed, optional=True)
                    tool_results.append(record)
                    artifacts.extend(record.pop("_artifacts", []))
                    rendered = _render_observation(record)
                    observations.append(rendered)
                    messages.append(Message("tool", rendered, tool_call_id=call.id))

            failures = [item for item in tool_results if not item.get("ok")]
            result.ok = not failures
            result.output = text or _fallback_output(tool_results)
            if failures:
                result.error = "; ".join(str(item.get("error")) for item in failures if item.get("error"))
                result.error_code = str(failures[0].get("error_code") or "tool_error")
            result.provider_id = provider_id
            result.model = model
            result.iterations = iterations
            result.usage = usage
            result.tool_results = tool_results
            result.artifacts = artifacts
            return result

        except asyncio.CancelledError:
            result.ok = False
            result.error = "execution cancelled"
            result.error_code = "task_cancelled"
            result.tool_results = tool_results
            return result
        except (PermissionDeniedError, ProviderError, ToolError):
            raise
        except Exception as exc:
            raise BackendError(
                f"native backend failed: {exc}", backend=self.id, cause=type(exc).__name__
            ) from exc
        finally:
            self._running.pop(request.execution_id, None)
            result.duration_ms = (time.perf_counter() - started) * 1000

    async def stream(self, request: ExecutionRequest) -> AsyncIterator[str]:
        provider_id = request.provider_id or request.agent.provider_id
        provider = self.providers.get(provider_id) if provider_id else self.providers.default
        if not provider.capabilities.streaming:
            raise BackendError(
                f"provider '{provider.id}' does not support streaming", backend=self.id, provider=provider.id
            )
        generation_request = GenerationRequest(
            messages=self._messages(request, []),
            model=request.model or request.agent.model or "",
            temperature=request.temperature,
            timeout=request.timeout,
        )
        async for chunk in provider.stream(generation_request):
            yield chunk

    async def cancel(self, execution_id: str) -> bool:
        event = self._running.get(execution_id)
        if event is None:
            return False
        event.set()
        return True

    async def health_check(self) -> HealthStatus:
        if len(self.providers) == 0:
            return HealthStatus(HealthState.UNHEALTHY, "no providers registered", time.time())
        if len(self.tools) == 0:
            return HealthStatus(HealthState.DEGRADED, "no tools registered", time.time())
        return HealthStatus(
            HealthState.HEALTHY,
            f"{len(self.providers)} provider(s), {len(self.tools)} tool(s)",
            time.time(),
        )

    # -- internals -------------------------------------------------------------

    def _allowed_tools(self, request: ExecutionRequest) -> frozenset[str]:
        allowed = set(request.allowed_tools or request.agent.tools)
        if self.tool_filter is not None:
            allowed &= set(self.tool_filter)
        return frozenset(name for name in allowed if self.tools.has(name))

    def _messages(self, request: ExecutionRequest, observations: list[str]) -> list[Message]:
        messages = list(request.messages())
        for observation in observations[-8:]:
            messages.append(Message("tool", observation))
        return messages

    async def _invoke(
        self,
        request: ExecutionRequest,
        name: str,
        arguments: dict[str, Any],
        allowed: frozenset[str],
        *,
        optional: bool,
    ) -> dict[str, Any]:
        """Run one tool through the registry, converting failures into records."""
        context = self.tools.context(
            workflow_id=request.workflow_id,
            task_id=request.task_id,
            agent_id=request.agent.id,
            timeout=min(request.timeout, 120.0),
        )
        try:
            result = await self.tools.call(
                name,
                arguments,
                context=context,
                agent_permissions=request.agent.permissions,
                agent_tools=allowed,
                approval_handler=request.approval_handler,
            )
        except ApprovalRequiredError as exc:
            record = _failure_record(name, arguments, exc)
            if not optional:
                raise
            return record
        except PermissionDeniedError as exc:
            record = _failure_record(name, arguments, exc)
            if not optional:
                raise
            return record
        except ToolError as exc:
            record = _failure_record(name, arguments, exc)
            if not optional:
                raise
            return record

        record = {
            "tool": name,
            "arguments": arguments,
            "ok": result.ok,
            "output": result.output,
            "error": result.error,
            "duration_ms": round(result.duration_ms, 2),
            "metadata": result.metadata,
            "_artifacts": [
                {**descriptor, "task_id": request.task_id, "creator": request.agent.id}
                for descriptor in result.artifacts
            ],
        }
        return record


def _failure_record(name: str, arguments: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "tool": name,
        "arguments": arguments,
        "ok": False,
        "output": None,
        "error": str(exc),
        "error_code": getattr(exc, "code", "tool_error"),
        "duration_ms": 0.0,
        "metadata": getattr(exc, "details", {}),
        "_artifacts": [],
    }


def _render_observation(record: dict[str, Any]) -> str:
    payload = {
        "tool": record.get("tool"),
        "ok": record.get("ok"),
        "output": record.get("output"),
        "error": record.get("error"),
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_OBSERVATION_CHARS else text[:MAX_OBSERVATION_CHARS] + "…[truncated]"


def _fallback_output(tool_results: list[dict[str, Any]]) -> str:
    if not tool_results:
        return ""
    return json.dumps(
        [{"tool": item.get("tool"), "ok": item.get("ok"), "output": item.get("output")} for item in tool_results],
        ensure_ascii=False,
        default=str,
    )[:MAX_OBSERVATION_CHARS]


def _parse_inline_tool_call(text: str) -> list[ToolCall]:
    """Support local models that emit ``{"tool": ..., "arguments": {...}}`` as text."""
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return []
    try:
        payload = json.loads(stripped)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    name = payload.get("tool") or payload.get("name")
    arguments = payload.get("arguments", payload.get("input", {}))
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return []
    return [ToolCall(name=name, arguments=arguments)]


class MCPBackend(NativeBackend):
    """Native runtime restricted to tools discovered from MCP servers."""

    type = BackendType.MCP
    description = "Executes agents against tools exposed by MCP servers."

    def __init__(self, providers: ProviderRegistry, tools: ToolRegistry, *, id: str = "mcp", **overrides: Any) -> None:
        super().__init__(providers, tools, id=id, **overrides)

    def _allowed_tools(self, request: ExecutionRequest) -> frozenset[str]:
        allowed = super()._allowed_tools(request)
        return frozenset(name for name in allowed if "mcp" in self.tools.get(name).tags)

    async def health_check(self) -> HealthStatus:
        mcp_tools = [tool for tool in self.tools if "mcp" in tool.tags]
        if not mcp_tools:
            return HealthStatus(HealthState.UNHEALTHY, "no MCP tools discovered", time.time())
        return HealthStatus(HealthState.HEALTHY, f"{len(mcp_tools)} MCP tool(s)", time.time())


__all__ = ["MCPBackend", "NativeBackend"]
