"""Shared fixtures.

Every fixture keeps its state inside a temporary directory so tests never touch
``~/.airvis`` and never depend on execution order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from airvis.agents.defaults import default_agents
from airvis.agents.registry import AgentRegistry
from airvis.agents.router import AgentRouter
from airvis.artifacts.manager import ArtifactManager
from airvis.backends.factory import build_backend_registry
from airvis.context.manager import ContextManager
from airvis.core.config import AirvisConfig
from airvis.core.events import EventBus
from airvis.core.health import HealthRegistry
from airvis.engine import AirvisEngine
from airvis.providers.mock import MockProvider
from airvis.providers.registry import ProviderRegistry
from airvis.security.permissions import PermissionManager, always_approve
from airvis.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def isolate_airvis_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Prevent developer machine configuration from changing test routing.

    AIRVIS production intentionally reads ``~/.airvis`` and provider credentials.
    The test suite must not accidentally inherit those settings, otherwise a
    developer with OpenRouter/OpenClaw configured can make deterministic tests
    route into the real network/backend.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in (
        "AIRVIS_CONFIG",
        "AIRVIS_PROVIDER",
        "AIRVIS_MODEL",
        "AIRVIS_FALLBACK_PROVIDER",
        "AIRVIS_PROVIDER_TIMEOUT",
        "AIRVIS_BACKENDS",
        "AIRVIS_CHANNELS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "OLLAMA_HOST",
        "OLLAMA_MODEL",
        "OPENCLAW_CLI",
        "HERMES_CLI",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "sample.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    return root


@pytest.fixture
def config(workspace: Path) -> AirvisConfig:
    settings = AirvisConfig()
    settings.workspace = str(workspace)
    settings.state.enabled = False
    settings.workflow.persist = False
    settings.workflow.max_concurrency = 4
    return settings


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def health() -> HealthRegistry:
    return HealthRegistry()


@pytest.fixture
def permissions(config: AirvisConfig, workspace: Path, event_bus: EventBus) -> PermissionManager:
    return PermissionManager(config.security, workspace, event_bus=event_bus)


@pytest.fixture
def tools(workspace: Path, permissions: PermissionManager, event_bus: EventBus) -> ToolRegistry:
    return ToolRegistry(workspace, permissions=permissions, event_bus=event_bus)


@pytest.fixture
def providers(health: HealthRegistry, event_bus: EventBus) -> ProviderRegistry:
    return ProviderRegistry([MockProvider()], health=health, event_bus=event_bus, fallbacks=["mock"])


@pytest.fixture
def backends(config, providers, tools, workspace, health, event_bus):
    return build_backend_registry(
        config, providers, tools, workspace=workspace, health=health, event_bus=event_bus
    )


@pytest.fixture
def agents(config, backends, providers, tools, health) -> AgentRegistry:
    registry = AgentRegistry(backends=backends, providers=providers, tools=tools, health=health)
    registry.register_all(
        default_agents(providers=providers, backends=backends, tools=tools, config=config.agents)
    )
    return registry


@pytest.fixture
def router(agents, config, providers, backends, tools, health, event_bus) -> AgentRouter:
    return AgentRouter(
        agents,
        config=config.routing,
        providers=providers,
        backends=backends,
        tools=tools,
        health=health,
        event_bus=event_bus,
    )


@pytest.fixture
def artifacts(tmp_path: Path, event_bus: EventBus) -> ArtifactManager:
    return ArtifactManager(tmp_path / "artifacts", event_bus=event_bus)


@pytest.fixture
def context(config, artifacts, workspace) -> ContextManager:
    return ContextManager(config.context, artifacts=artifacts, workspace=workspace)


@pytest.fixture
def engine(tmp_path: Path, workspace: Path, config: AirvisConfig) -> AirvisEngine:
    """A fully wired engine confined to the temporary workspace."""
    config.state.enabled = True
    config.workflow.persist = True
    return AirvisEngine(
        config,
        workspace=workspace,
        approval_handler=always_approve,
        state_path=tmp_path / "state.db",
        memory_path=tmp_path / "memory.db",
        artifact_root=tmp_path / "artifacts",
        environ={},
    )
