"""Composition root for the AIRVIS orchestration engine."""

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
from .core.events import EventBus
from .core.health import HealthRegistry
from .orchestration.dag import DAGEngine
from .orchestration.orchestrator import Orchestrator
from .orchestration.planner import LLMPlanner, Planner
from .orchestration.repair import ErrorAnalyzer, RepairPlanner
from .orchestration.review import ReviewSystem
from .orchestration.task import WorkflowResult
from .plugins import PluginManager
from .providers.factory import build_provider_registry
from .providers.registry import ProviderRegistry
from .security.permissions import PermissionManager
from .skills import SkillRegistry
from .state.store import MemoryStore, StateStore
from .tools.registry import ToolRegistry

log = logging.getLogger("airvis.engine")


class AirvisEngine:
    """Native AIRVIS orchestration engine."""

    def __init__(self, config: AirvisConfig | None = None, *, workspace: Path | str | None = None,
                 environ: dict[str, str] | None = None, approval_handler: Any = None,
                 state_path: Path | str | None = None, memory_path: Path | str | None = None,
                 artifact_root: Path | str | None = None, register_default_agents: bool = True,
                 use_llm_planner: bool = True) -> None:
        if config is not None:
            self.config = config
        else:
            root = Path(workspace).resolve() if workspace is not None else Path.cwd()
            self.config = AirvisConfig.load(environ=environ, search_from=root)
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
        self.permissions = PermissionManager(self.config.security, self.workspace,
                                             event_bus=self.event_bus, approval_handler=approval_handler)
        self.tools = ToolRegistry(self.workspace, permissions=self.permissions, event_bus=self.event_bus)
        self.providers: ProviderRegistry = build_provider_registry(
            self.config, environ=environ, health=self.health, event_bus=self.event_bus)
        self.plugins = PluginManager()
        self.plugin_load_results = self.plugins.load_enabled(
            tools=self.tools, providers=self.providers, event_bus=self.event_bus)
        self.skills = SkillRegistry()
        self.backends: BackendRegistry = build_backend_registry(
            self.config, self.providers, self.tools, workspace=self.workspace,
            health=self.health, event_bus=self.event_bus)
        self.agents = AgentRegistry(backends=self.backends, providers=self.providers,
                                    tools=self.tools, health=self.health)
        if register_default_agents:
            self.agents.register_all(default_agents(providers=self.providers, backends=self.backends,
                                                    tools=self.tools, config=self.config.agents))
        self.router = AgentRouter(self.agents, config=self.config.routing, providers=self.providers,
                                  backends=self.backends, tools=self.tools, health=self.health,
                                  event_bus=self.event_bus)
        self.context = ContextManager(self.config.context, artifacts=self.artifacts,
                                      workspace=self.workspace, memory=self.memory)
        self.planner: Planner = (
            LLMPlanner(self.providers, tools=self.tools, agents_config=self.config.agents,
                       workflow_config=self.config.workflow)
            if use_llm_planner else Planner(agents_config=self.config.agents, workflow_config=self.config.workflow)
        )
        self.dag = DAGEngine(config=self.config.workflow, event_bus=self.event_bus, store=self.store)
        self.review = ReviewSystem(self.config.review, providers=self.providers,
                                   artifacts=self.artifacts, event_bus=self.event_bus)
        self.orchestrator = Orchestrator(
            config=self.config, agents=self.agents, router=self.router, backends=self.backends,
            providers=self.providers, tools=self.tools, permissions=self.permissions,
            planner=self.planner, dag=self.dag, review=self.review, context=self.context,
            artifacts=self.artifacts, event_bus=self.event_bus, health=self.health, store=self.store,
            analyzer=ErrorAnalyzer(), repair_planner=RepairPlanner(self.config.repair),
            approval_handler=approval_handler)
        if self.store.enabled:
            self.event_bus.subscribe(self._persist_event)
        if self.config.mcp.enabled:
            self._install_mcp()

    async def run(self, request: str, **kwargs: Any) -> WorkflowResult:
        return await self.orchestrator.run(request, **kwargs)

    def run_sync(self, request: str, **kwargs: Any) -> WorkflowResult:
        return run_blocking(self.orchestrator.run(request, **kwargs))

    async def resume(self, workflow_id: str) -> WorkflowResult:
        return await self.orchestrator.resume(workflow_id)

    def cancel(self, workflow_id: str) -> bool:
        return self.orchestrator.cancel(workflow_id)

    async def health_check(self) -> dict[str, Any]:
        return {"providers": await self.providers.health_check_all(),
                "backends": await self.backends.health_check_all()}

    def describe(self) -> dict[str, Any]:
        return {"workspace": str(self.workspace), "config_source": self.config.source,
                "routing_strategy": self.config.routing.strategy, "providers": self.providers.names(),
                "backends": self.backends.names(), "agents": self.agents.names(),
                "tools": len(self.tools), "plugins": self.plugins.list(), "skills": self.skills.list(),
                "persistence": self.store.enabled, "mcp": self.config.mcp.enabled,
                "native_primary": self.backends.has("native"), "planner": type(self.planner).__name__}

    async def close(self) -> None:
        await self.backends.close()
        self.store.close()

    def _persist_event(self, event: Any) -> None:
        if self.store.enabled:
            try:
                self.store.record_event(event)
            except Exception:
                log.exception("failed to persist AIRVIS event")

    def _install_mcp(self) -> None:
        try:
            from .mcp import install_mcp_servers
            install_mcp_servers(self)
        except ImportError:
            log.warning("MCP is enabled but the optional MCP runtime is unavailable")


def build_engine(config: AirvisConfig | None = None, *, workspace: Path | str | None = None,
                 **kwargs: Any) -> AirvisEngine:
    """Compatibility factory used by the public AIRVIS package API."""
    return AirvisEngine(config, workspace=workspace, **kwargs)


__all__ = ["AirvisEngine", "build_engine"]
