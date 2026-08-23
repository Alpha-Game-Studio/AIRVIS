"""Backend interface: the layer that decides *how an agent executes*.

A backend owns the execution environment, tool access, session and context
handling, streaming and cancellation. It delegates text generation to the
provider layer and never re-implements it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..agents.spec import AgentSpec
from ..context.manager import ContextBundle
from ..core.errors import BackendError
from ..core.health import HealthState, HealthStatus

if TYPE_CHECKING:  # pragma: no cover - typing only; backends never import orchestration
    from ..orchestration.task import ToolStep


class BackendType(str, Enum):
    NATIVE = "native"
    OPENCLAW = "openclaw"
    HERMES = "hermes"
    MCP = "mcp"
    CUSTOM = "custom"


@dataclass
class ExecutionRequest:
    """Everything a backend needs to run one task for one agent."""

    agent: AgentSpec
    instruction: str
    context: ContextBundle | None = None
    execution_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str | None = None
    workflow_id: str | None = None
    #: tool names the agent may call during this execution
    allowed_tools: frozenset[str] = frozenset()
    #: deterministic tool steps attached by the planner
    tool_plan: list[ToolStep] = field(default_factory=list)
    provider_id: str | None = None
    model: str | None = None
    timeout: float = 300.0
    max_iterations: int = 4
    temperature: float = 0.2
    approval_handler: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def messages(self) -> list[Any]:
        from ..providers.base import Message

        if self.context is None:
            return [Message("user", self.instruction)]
        bundle = self.context
        bundle.task = bundle.task or self.instruction
        return bundle.to_messages()


@dataclass
class ExecutionResult:
    """Structured outcome of one backend execution."""

    ok: bool = True
    output: str = ""
    error: str | None = None
    error_code: str | None = None
    backend_id: str = ""
    provider_id: str | None = None
    model: str | None = None
    execution_id: str = ""
    iterations: int = 0
    duration_ms: float = 0.0
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "error_code": self.error_code,
            "backend_id": self.backend_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "execution_id": self.execution_id,
            "iterations": self.iterations,
            "duration_ms": round(self.duration_ms, 2),
            "tool_results": self.tool_results,
            "artifacts": self.artifacts,
            "usage": self.usage,
            "metadata": self.metadata,
        }


class Backend:
    """Base class for every execution backend."""

    id: str = ""
    type: BackendType = BackendType.CUSTOM
    #: coarse feature flags, e.g. {"tools", "streaming", "cancel", "sessions"}
    capabilities: frozenset[str] = frozenset({"chat"})
    description: str = ""

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            setattr(self, key, value)
        if not self.id:
            raise ValueError(f"{type(self).__name__} must define an id")

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:  # pragma: no cover - abstract
        raise NotImplementedError(f"backend {self.id} does not implement execute()")

    def stream(self, request: ExecutionRequest) -> AsyncIterator[str]:
        """Return an async iterator of output chunks (see :meth:`Provider.stream`)."""
        if "streaming" not in self.capabilities:
            raise BackendError(f"backend {self.id} does not support streaming", backend=self.id)
        raise NotImplementedError(f"backend {self.id} declares streaming but does not implement it")

    async def cancel(self, execution_id: str) -> bool:
        """Cancel a running execution; returns True when something was cancelled."""
        return False

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.UNKNOWN, "no health check implemented", time.time())

    async def close(self) -> None:
        """Release long-lived resources (sessions, subprocesses, sockets)."""

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "class": type(self).__name__,
            "capabilities": sorted(self.capabilities),
            "description": self.description,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Backend {self.id} type={self.type.value}>"


__all__ = ["Backend", "BackendType", "ExecutionRequest", "ExecutionResult"]
