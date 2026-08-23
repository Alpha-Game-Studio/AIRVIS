"""Agent declaration, registry and routing."""

from .defaults import DEFAULT_ROSTER, default_agents
from .registry import AgentRegistry
from .router import AgentRouter, RoutingCandidate, RoutingDecision
from .spec import AgentSpec

__all__ = [
    "DEFAULT_ROSTER",
    "AgentRegistry",
    "AgentRouter",
    "AgentSpec",
    "RoutingCandidate",
    "RoutingDecision",
    "default_agents",
]
