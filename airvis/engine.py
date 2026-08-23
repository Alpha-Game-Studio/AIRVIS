"""Composition root.

:class:`AirvisEngine` wires the registries, routers and pipeline stages
together from a single :class:`~airvis.core.config.AirvisConfig`. Every other
entry point (CLI, HTTP server, legacy runtime) builds on this one object.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .agents.defaults import default_agents
from .agents.registry import AgentRegistry
from .agents.router import AgentRouter
from .artifacts.manager import ArtifactManager
from .backends.factory import build_backend_registry
from .backends.registry import BackendRegistry
from .context.manager import ContextManager
from .core.asyncutil import run_blocking
from .core.config import AirvisConfig
from .core.events import EventBus, EventType
from .core.health import HealthRegistry
from .orchestration.dag import DAGEngine
from .orchestration.orchestrator import Orchestrator
from .orchestration.planner import LLMPlanner, Planner
from .orchestration.repair import ErrorAnalyzer, RepairPlanner
from .orchestration.review import ReviewSystem
from .orchestration.task import WorkflowResult
from .providers.factory import build_provider_registry
from .providers.registry import ProviderRegistry
from .security.permissions import PermissionManager
from .state.store import MemoryStore, StateStore
from .tools.registry import ToolRegistry

log = logging.getLogger("airvis.engine")


class AirvisEngine:
    """The assembled AIRVIS orchestration engine."""

    def __init__(
        self,
        config: AirvisConfig | None = None,
        *,
        workspace: Path | str | None = None,
        environ: dict[str, str] | None = None,
        approval_handler: Any = None,
        state_path: Path | str | None = None,
        memory_path: Path | str | None = None,
        artifact_root: Path | str | None = None,
        register_default_agents: bool = True,
        use_llm_planner: bool = False,
    ) -> None:
        self.config = config or AirvisConfig.load(environ=environ)
        if workspace is not None:
            self.config.workspace = str(Path(workspace).resolve())
        self.workspace = Path(self.config.workspace).resolve()

        self.event_bus = EventBus()
        self.health = HealthRegistry()

        self.store = StateStore(
            state_path or (self.config.state.resolved_path() if self.config.state.path else None),
            enabled=self.config.state.enabled and self.config.workflow.persist,
        )
        self.memory = MemoryStore(memory_path)
        self.artifacts = ArtifactManager(artifact_root, event_bus=self.event_bus)

        self.permissions = PermissionManager(
            self.config.security,
            self.workspace,
            event_bus=self.event_bus,
            approval_handler=approval_handler,
        )
        self.tools = ToolRegistry(self.workspace, permissions=self.permissions, event_bus=self.event_bus)

        self.providers: ProviderRegistry = build_provider_registry(
            self.config, environ=environ, health=self.health, event_bus=self.event_bus
        )
        self.backends: BackendRegistry = build_backend_registry(
            self.config, self.providers, self.tools,
            workspace=self.workspace, health=self.health, event_bus=self.event_bus,
        )
        self.agents = AgentRegistry(
            backends=self.backends, providers=self.providers, tools=self.tools, health=self.health
        )
        if register_default_agents:
            self.agents.register_all(
                default_agents(
                    providers=self.providers,
                    backends=self.backends,
                    tools=self.tools,
                    config=self.config.agents,
                )
            )

        self.router = AgentRouter(
            self.agents,
            config=self.config.routing,
            providers=self.providers,
            backends=self.backends,
            tools=self.tools,
            health=self.health,
            event_bus=self.event_bus,
        )
        self.context = ContextManager(
            self.config.context, artifacts=self.artifacts, workspace=self.workspace, memory=self.memory
        )
        self.planner: Planner = (
            LLMPlanner(
                self.providers,
                tools=self.tools,
                agents_config=self.config.agents,
                workflow_config=self.config.workflow,
            )
            if use_llm_planner
            else Planner(agents_config=self.config.agents, workflow_config=self.config.workflow)
        )
        self.dag = DAGEngine(config=self.config.workflow, event_bus=self.event_bus, store=self.store)
        self.review = ReviewSystem(
            self.config.review, providers=self.providers, artifacts=self.artifacts, event_bus=self.event_bus
        )
        self.orchestrator = Orchestrator(
            config=self.config,
            agents=self.agents,
            router=self.router,
            backends=self.backends,
            providers=self.providers,
            tools=self.tools,
            permissions=self.permissions,
            planner=self.planner,
            dag=self.dag,
            review=self.review,
            context=self.context,
            artifacts=self.artifacts,
            event_bus=self.event_bus,
            health=self.health,
            store=self.store,
            analyzer=ErrorAnalyzer(),
            repair_planner=RepairPlanner(self.config.repair),
            approval_handler=approval_handler,
        )

        if self.store.enabled:
            self.event_bus.subscribe(self._persist_event)

        if self.config.mcp.enabled:
            self._install_mcp()

    # -- execution -------------------------------------------------------------

    async def run(self, request: str, **kwargs: Any) -> WorkflowResult:
        return await self.orchestrator.run(request, **kwargs)

    def run_sync(self, request: str, **kwargs: Any) -> WorkflowResult:
        return run_blocking(self.orchestrator.run(request, **kwargs))

    async def resume(self, workflow_id: str) -> WorkflowResult:
        return await self.orchestrator.resume(workflow_id)

    def cancel(self, workflow_id: str) -> bool:
        return self.orchestrator.cancel(workflow_id)

    # -- introspection ---------------------------------------------------------

    async def health_check(self) -> dict[str, Any]:
        return {
            "providers": await self.providers.health_check_all(),
            "backends": await self.backends.health_check_all(),
            "agents": {
                agent.id: {"problems": self.agents.reference_problems(agent)} for agent in self.agents.all()
            },
            "tools": len(self.tools),
            "workspace": str(self.workspace),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "config_source": self.config.source,
            "routing_strategy": self.config.routing.strategy,
            "providers": self.providers.names(),
            "backends": self.backends.names(),
            "agents": self.agents.names(),
            "tools": len(self.tools),
            "persistence": self.store.enabled,
            "mcp": self.config.mcp.enabled,
        }

    async def close(self) -> None:
        from .mcp.integration import close_mcp_tools

        await close_mcp_tools(self.tools)
        await self.backends.close()

    # -- internals -------------------------------------------------------------

    def _persist_event(self, event: Any) -> None:
        try:
            self.store.save_event(event.to_dict())
        except Exception:  # persistence is best-effort
            log.debug("failed to persist event", exc_info=True)

    def _install_mcp(self) -> None:
        from .mcp.integration import register_mcp_tools

        try:
            discovered = run_blocking(register_mcp_tools(self.config.mcp, self.tools))
        except Exception as exc:
            log.warning("MCP discovery failed: %s", exc)
            return
        if discovered:
            self.event_bus.publish(
                EventType.HEALTH_CHANGED, status="mcp_ready", metadata={"tools": discovered}
            )


def build_engine(**kwargs: Any) -> AirvisEngine:
    """Convenience factory used by the CLI and the HTTP server."""
    return AirvisEngine(**kwargs)


__all__ = ["AirvisEngine", "build_engine"]
