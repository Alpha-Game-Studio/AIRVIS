"""First-class OpenClaw runtime built on AIRVIS orchestration.

This is intentionally *not* an adapter around the external ``openclaw`` CLI.
OpenClaw mode is an autonomous agent runtime: AIRVIS plans work, builds a DAG,
routes each task to an agent, executes real tools, records artifacts/context,
reviews the result and repairs failed tasks.

The external OpenClaw binary remains optional compatibility infrastructure. The
class in this module is the OpenClaw implementation users can run directly.
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


class OpenClaw:
    """Autonomous, orchestrated coding/desktop agent runtime.

    Unlike the legacy backend integration, this object owns the full agent loop.
    A single user request can become multiple dependent tasks and can recover
    from failed implementation/test/review stages without handing control to an
    external CLI process.
    """

    name = "AIRVIS OpenClaw"
    version = "7.0.0"

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
        if not self.options.auto_repair:
            settings.repair.max_retries = 0

        self.engine = AirvisEngine(
            settings,
            workspace=root,
            approval_handler=approval_handler,
            use_llm_planner=self.options.use_llm_planner,
        )
        self.workspace = root
        self.session_id: str | None = None

    async def run(self, request: str) -> WorkflowResult:
        """Execute a request through the complete autonomous OpenClaw loop."""
        result = await self.engine.run(request, strategy=self.options.strategy)
        self.session_id = result.workflow_id
        return result

    def run_sync(self, request: str) -> WorkflowResult:
        """Synchronous convenience API for desktop/CLI integrations."""
        return run_blocking(self.run(request))

    async def resume(self, workflow_id: str) -> WorkflowResult:
        """Resume a persisted workflow without losing its task graph."""
        result = await self.engine.resume(workflow_id)
        self.session_id = result.workflow_id
        return result

    def cancel(self, workflow_id: str | None = None) -> bool:
        """Cancel the active workflow or an explicitly supplied workflow."""
        target = workflow_id or self.session_id
        if not target:
            return False
        return self.engine.cancel(target)

    async def events(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        """Return the persisted/in-memory event trail for a workflow."""
        target = workflow_id or self.session_id
        if not target:
            return []
        return self.engine.event_bus.history(workflow_id=target, limit=400)

    async def stream(self, request: str) -> AsyncIterator[dict[str, Any]]:
        """Yield orchestration events while a request is executing.

        The event bus is intentionally used instead of scraping subprocess
        output, so consumers receive structured task/agent/tool/review/repair
        events regardless of the selected model.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        finished = asyncio.Event()

        def on_event(event: Event) -> None:
            payload = event.to_dict()
            try:
                queue.put_nowait(payload)
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
        """Describe this runtime and its orchestration capabilities."""
        return {
            "name": self.name,
            "version": self.version,
            "mode": "native_orchestrated",
            "workspace": str(self.workspace),
            "capabilities": [
                "planning",
                "multi_agent",
                "dag_orchestration",
                "tool_execution",
                "context",
                "artifacts",
                "review",
                "repair",
                "resume",
                "sessions",
                "permissions",
                "streaming_events",
            ],
            "agents": [agent.id for agent in self.engine.agents.all()],
            "tools": len(self.engine.tools),
        }


__all__ = ["OpenClaw", "OpenClawOptions"]
