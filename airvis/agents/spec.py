"""Agent declaration.

An agent is configuration, not code: it names the backend that runs it, the
provider/model that generates for it, the tools it may touch and the
capabilities it advertises. Execution never infers these from the agent id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentSpec:
    id: str
    role: str = ""
    description: str = ""
    #: capabilities this agent advertises; matched against ``Task.required_capabilities``
    capabilities: frozenset[str] = frozenset()
    #: tools the agent is permitted to use — enforced by the permission manager
    tools: frozenset[str] = frozenset()
    #: permissions granted to the agent, checked against ``Tool.required_permissions``
    permissions: frozenset[str] = frozenset()
    #: explicit execution references; never derived from ``id``
    backend_id: str = "native"
    provider_id: str | None = None
    model: str | None = None
    system_prompt: str = ""
    priority: float = 1.0
    max_concurrency: int = 4
    timeout: float = 300.0
    max_iterations: int = 4
    #: quality prior in ``[0, 1]`` used by the QUALITY/PREMIUM routing strategies
    quality: float = 0.5
    enabled: bool = True
    tags: frozenset[str] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("agent id is required")
        self.role = self.role or self.id
        self.capabilities = frozenset(self.capabilities)
        self.tools = frozenset(self.tools)
        self.permissions = frozenset(self.permissions)
        self.tags = frozenset(self.tags)

    def covers(self, required: list[str] | set[str] | frozenset[str]) -> bool:
        return set(required).issubset(self.capabilities)

    def capability_match(self, required: list[str] | set[str] | frozenset[str]) -> float:
        """Fraction of required capabilities this agent advertises."""
        wanted = set(required)
        if not wanted:
            return 1.0
        return len(wanted & self.capabilities) / len(wanted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "description": self.description,
            "capabilities": sorted(self.capabilities),
            "tools": sorted(self.tools),
            "permissions": sorted(self.permissions),
            "backend_id": self.backend_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "priority": self.priority,
            "max_concurrency": self.max_concurrency,
            "timeout": self.timeout,
            "quality": self.quality,
            "enabled": self.enabled,
            "tags": sorted(self.tags),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentSpec:
        return cls(
            id=str(data["id"]),
            role=str(data.get("role", "")),
            description=str(data.get("description", "")),
            capabilities=frozenset(data.get("capabilities") or ()),
            tools=frozenset(data.get("tools") or ()),
            permissions=frozenset(data.get("permissions") or ()),
            backend_id=str(data.get("backend_id", "native")),
            provider_id=data.get("provider_id"),
            model=data.get("model"),
            system_prompt=str(data.get("system_prompt", "")),
            priority=float(data.get("priority", 1.0)),
            max_concurrency=int(data.get("max_concurrency", 4)),
            timeout=float(data.get("timeout", 300.0)),
            max_iterations=int(data.get("max_iterations", 4)),
            quality=float(data.get("quality", 0.5)),
            enabled=bool(data.get("enabled", True)),
            tags=frozenset(data.get("tags") or ()),
            metadata=dict(data.get("metadata") or {}),
        )


__all__ = ["AgentSpec"]
