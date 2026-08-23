"""Assemble the backend registry from configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.config import AirvisConfig, BackendsConfig
from ..providers.registry import ProviderRegistry
from ..tools.registry import ToolRegistry
from .cli import HermesBackend, OpenClawBackend
from .native import MCPBackend, NativeBackend
from .registry import BackendRegistry


def build_backend_registry(
    config: AirvisConfig | BackendsConfig | None,
    providers: ProviderRegistry,
    tools: ToolRegistry,
    *,
    workspace: Path | str | None = None,
    health: Any = None,
    event_bus: Any = None,
) -> BackendRegistry:
    """Register every enabled backend. ``native`` is always available."""
    settings = config.backends if isinstance(config, AirvisConfig) else (config or BackendsConfig())
    root = Path(workspace or (config.workspace if isinstance(config, AirvisConfig) else Path.cwd())).resolve()
    registry = BackendRegistry(health=health, event_bus=event_bus)

    enabled = {item.strip().lower() for item in settings.enabled if item.strip()}
    enabled.add("native")

    if "native" in enabled:
        registry.register(
            NativeBackend(providers, tools, max_tool_calls=settings.max_tool_calls)
        )
    if "openclaw" in enabled:
        registry.register(
            OpenClawBackend(settings.openclaw_command, workspace=root, timeout=settings.execute_timeout)
        )
    if "hermes" in enabled:
        registry.register(
            HermesBackend(settings.hermes_command, workspace=root, timeout=settings.execute_timeout)
        )
    if "mcp" in enabled:
        registry.register(MCPBackend(providers, tools))
    return registry


__all__ = ["build_backend_registry"]
