"""First-class OpenClaw runtime built on AIRVIS orchestration.

This module implements OpenClaw natively: planning, DAG orchestration, agent
routing, real tool execution, context, review and repair all happen inside the
AIRVIS process. External OpenClaw/Hermes binaries are optional compatibility
backends, not the OpenClaw implementation itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .core.asyncutil import run_blocking
from .core.config import AirvisConfig
from .core.events import Event
from .engine import AirvisEngine
from .orchestration.task import WorkflowResult


@dataclass(frozen=True)
class OpenClawOptions:
    """Execution policy for the autonomous OpenClaw runtime."""

    max_iterations: int = 8
    max_tool_calls: int = 40
    use_llm_planner: bool = True
    strategy: str = "balanced"
    auto_repair: bool = True
    provider: str | None = None
    model: str | None = None


class OpenClaw:
    """Autonomous, orchestrated coding/desktop agent runtime.

    A request becomes a real task graph. Each task is routed to an agent, the
    native backend gives that agent access to AIRVIS tools, provider calls are
    made through the provider registry, and review/repair can iterate until the
    workflow reaches a terminal state.
    """

    name = "AIRVIS OpenClaw"
    version = "7.1.0"

    def __init__(
        self,
        workspace: Path | str | None = None,
        *,
        config: AirvisConfig | None = None,
        options: OpenClawOptions | None = None,
        approval_handler: Any = None,
    ) -> None:
        self.options = options or OpenClawOptions()
        root = Path(workspace or Path.cwd()).resolve()
        settings = config or AirvisConfig.load()
        settings.workspace = str(root)
        settings.backends.enabled = ["native"]
        settings.backends.max_iterations = max(1, self.options.max_iterations)
        settings.backends.max_tool_calls = max(1, self.options.max_tool_calls)
        settings.workflow.max_concurrency = max(1, settings.workflow.max_concurrency)

        # Explicit CLI/API selection wins over environment/configuration. The
        # model is applied to every native agent so the whole orchestration run
        # uses a real model instead of silently falling back to MockProvider.
        if self.options.provider:
            settings.providers.default = self.options.provider.strip().lower()
        if self.options.model:
            settings.providers.model = self.options.model if hasattr(settings.providers, "model") else ""
            # Provider-specific model variables are still honoured by factory;
            # keep a generic override in the environment-free path below.
            settings.providers.fallbacks = [
                item for item in settings.providers.fallbacks if item != "mock"
            ]

        if not self.options.auto_repair:
            settings.repair.max_retries = 0

        self.engine = AirvisEngine(
            settings,
            workspace=root,
            approval_handler=approval_handler,
            use_llm_planner=self.options.use_llm_planner,
        )
        if self.options.model:
            # Providers expose one default model; make the explicit OpenClaw
            # model selection authoritative after registry construction.
            for provider in self.engine.providers:
                provider.default_model = self.options.model
        self.workspace = root
        self.session_id: str | None = None

    async def run(self, request: str) -> WorkflowResult:
        result = await self.engine.run(request, strategy=self.options.strategy)
        self.session_id = result.workflow_id
        return result

    def run_sync(self, request: str) -> WorkflowResult:
        return run_blocking(self.run(request))

    async def resume(self, workflow_id: str) -> WorkflowResult:
        result = await self.engine.resume(workflow_id)
        self.session_id = result.workflow_id
        return result

    def cancel(self, workflow_id: str | None = None) -> bool:
        target = workflow_id or self.session_id
        if not target:
            return False
        return self.engine.cancel(target)

    async def events(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        target = workflow_id or self.session_id
        if not target:
            return []
        return self.engine.event_bus.history(workflow_id=target, limit=400)

    async def stream(self, request: str) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        finished = asyncio.Event()

        def on_event(event: Event) -> None:
            try:
                queue.put_nowait(event.to_dict())
            except asyncio.QueueFull:
                pass

        self.engine.event_bus.subscribe(on_event)

        async def execute() -> None:
            try:
                await self.run(request)
            finally:
                finished.set()

        task = asyncio.create_task(execute())
        try:
            while not finished.is_set() or not queue.empty():
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                yield item
            await task
        finally:
            if not task.done():
                task.cancel()

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "mode": "native_orchestrated",
            "workspace": str(self.workspace),
            "provider_override": self.options.provider,
            "model_override": self.options.model,
            "capabilities": [
                "planning", "multi_agent", "dag_orchestration", "tool_execution",
                "context", "artifacts", "review", "repair", "resume", "sessions",
                "permissions", "streaming_events",
            ],
            "agents": [agent.id for agent in self.engine.agents.all()],
            "tools": len(self.engine.tools),
            "providers": self.engine.providers.names(),
        }


__all__ = ["OpenClaw", "OpenClawOptions"]
