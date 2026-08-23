"""Deprecated module kept for backward compatibility.

Agents are now declared in :mod:`airvis.agents` and selected by
:class:`airvis.agents.router.AgentRouter`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .agents.defaults import DEFAULT_ROSTER
from .compat import LegacyAgentDelegator, deprecated

__all__ = ["AgentDelegator", "LegacyAgentDelegator", "SubAgent"]


@dataclass
class SubAgent:
    role: str
    tools: set[str] = field(default_factory=set)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class AgentDelegator:
    """V4 delegator; mirrors the default roster without an engine attached."""

    def __init__(self) -> None:
        deprecated("airvis.multiagent.AgentDelegator", "airvis.agents.AgentRegistry")
        self.agents = {
            entry["role"]: SubAgent(entry["role"], set(entry["tools"])) for entry in DEFAULT_ROSTER
        }

    def list(self) -> list[dict[str, object]]:
        return [
            {"id": agent.id, "role": agent.role, "tools": sorted(agent.tools)}
            for agent in self.agents.values()
        ]

    def delegate(self, role: str, prompt: str, runtime) -> str:
        if role not in self.agents:
            raise KeyError(f"Unknown agent role: {role}")
        return runtime.run(f"[{role} agent] {prompt}")
