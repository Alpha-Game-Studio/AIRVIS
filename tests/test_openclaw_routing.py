from __future__ import annotations

from airvis.agents.defaults import default_agents
from airvis.backends.factory import build_backend_registry
from airvis.core.config import AirvisConfig
from airvis.core.events import EventBus
from airvis.core.health import HealthRegistry
from airvis.providers.mock import MockProvider
from airvis.providers.registry import ProviderRegistry
from airvis.security.permissions import PermissionManager
from airvis.tools.registry import ToolRegistry


def test_execution_agents_prefer_openclaw_when_enabled(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AirvisConfig()
    config.workspace = str(workspace)
    config.backends.enabled = ["native", "openclaw"]

    health = HealthRegistry()
    event_bus = EventBus()
    permissions = PermissionManager(config.security, workspace, event_bus=event_bus)
    tools = ToolRegistry(workspace, permissions=permissions, event_bus=event_bus)
    providers = ProviderRegistry([MockProvider()], health=health, event_bus=event_bus)
    backends = build_backend_registry(
        config, providers, tools, workspace=workspace, health=health, event_bus=event_bus
    )

    agents = default_agents(
        providers=providers,
        backends=backends,
        tools=tools,
        config=config.agents,
    )
    by_id = {agent.id: agent for agent in agents}

    assert by_id["coder"].backend_id == "openclaw"
    assert by_id["debugger"].backend_id == "openclaw"
    assert by_id["tester"].backend_id == "openclaw"
    assert by_id["committer"].backend_id == "openclaw"
    assert by_id["coder"].provider_id is None
    assert by_id["coder"].model is None


def test_execution_agents_fall_back_to_native_without_openclaw(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AirvisConfig()
    config.workspace = str(workspace)
    config.backends.enabled = ["native"]

    health = HealthRegistry()
    event_bus = EventBus()
    permissions = PermissionManager(config.security, workspace, event_bus=event_bus)
    tools = ToolRegistry(workspace, permissions=permissions, event_bus=event_bus)
    providers = ProviderRegistry([MockProvider()], health=health, event_bus=event_bus)
    backends = build_backend_registry(
        config, providers, tools, workspace=workspace, health=health, event_bus=event_bus
    )

    agents = default_agents(
        providers=providers,
        backends=backends,
        tools=tools,
        config=config.agents,
    )
    by_id = {agent.id: agent for agent in agents}

    assert by_id["coder"].backend_id == "native"
    assert by_id["coder"].provider_id == "mock"
    assert by_id["tester"].backend_id == "native"
