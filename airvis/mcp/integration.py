"""Register MCP server tools into the canonical AIRVIS tool registry.

Discovered tools go through exactly the same permission, risk and approval
pipeline as built-in tools — an MCP server cannot grant itself privileges.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.config import MCPConfig, MCPServerConfig
from ..tools.base import RiskLevel, Tool, ToolContext
from ..tools.registry import ToolRegistry
from .client import MCPClient

log = logging.getLogger("airvis.mcp")


class MCPTool(Tool):
    """A tool whose implementation lives in an MCP server."""

    def __init__(
        self,
        client: MCPClient,
        remote_name: str,
        description: str,
        parameters: dict[str, Any],
        risk: RiskLevel,
        *,
        server: str,
    ) -> None:
        self.client = client
        self.remote_name = remote_name
        self.name = f"mcp.{server}.{remote_name}"
        self.description = description or f"MCP tool '{remote_name}' from server '{server}'"
        self.parameters = parameters or {"type": "object", "properties": {}, "required": []}
        self.risk = risk
        self.required_permissions = frozenset({"mcp"})
        self.network = True
        self.tags = frozenset({"mcp", server})
        super().__init__()

    async def run(self, context: ToolContext, **arguments: Any) -> Any:
        return await self.client.call_tool(self.remote_name, arguments)

    async def close(self) -> None:
        await self.client.close()


async def close_mcp_tools(registry: ToolRegistry) -> None:
    """Shut down every MCP server connection this registry owns."""
    seen: set[int] = set()
    for tool in list(registry):
        if not isinstance(tool, MCPTool) or id(tool.client) in seen:
            continue
        seen.add(id(tool.client))
        try:
            await tool.close()
        except Exception:  # pragma: no cover - shutdown is best-effort
            log.debug("failed to close MCP client for %s", tool.name, exc_info=True)


async def discover_server(
    server: MCPServerConfig, registry: ToolRegistry, *, timeout: float = 20.0
) -> list[str]:
    """Connect to one server and register everything it exposes."""
    client = MCPClient(server.name, server.command, server.args, env=server.env, timeout=timeout)
    await client.connect()
    registered: list[str] = []
    for descriptor in await client.list_tools():
        risk = RiskLevel.parse(
            (descriptor.get("annotations") or {}).get("risk", server.default_risk), RiskLevel.MEDIUM
        )
        tool = MCPTool(
            client,
            str(descriptor["name"]),
            str(descriptor.get("description", "")),
            descriptor.get("inputSchema") or descriptor.get("input_schema") or {},
            risk,
            server=server.name,
        )
        registry.register(tool)
        registered.append(tool.name)
    return registered


async def register_mcp_tools(config: MCPConfig, registry: ToolRegistry) -> list[str]:
    """Discover every enabled MCP server; a broken server never breaks startup."""
    if not config.enabled:
        return []
    discovered: list[str] = []
    for server in config.servers:
        if not server.enabled:
            continue
        try:
            discovered.extend(await discover_server(server, registry, timeout=config.connect_timeout))
        except Exception as exc:
            log.warning("MCP server '%s' unavailable: %s", server.name, exc)
    return discovered


__all__ = ["MCPTool", "close_mcp_tools", "discover_server", "register_mcp_tools"]
