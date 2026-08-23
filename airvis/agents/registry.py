"""Agent registry with reference validation.

Registration fails loudly when an agent points at a backend, provider or tool
that does not exist — the alternative (discovering it mid-workflow) is exactly
the failure mode this rewrite removes.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Iterator
from typing import Any

from ..core.errors import DuplicateRegistrationError, InvalidReferenceError, UnknownAgentError
from ..core.health import HealthRegistry
from .spec import AgentSpec


class AgentRegistry:
    """Holds :class:`AgentSpec` objects and validates their references."""

    def __init__(
        self,
        agents: Iterable[AgentSpec] | None = None,
        *,
        backends: Any = None,
        providers: Any = None,
        tools: Any = None,
        health: HealthRegistry | None = None,
        validate: bool = True,
    ) -> None:
        self.backends = backends
        self.providers = providers
        self.tools = tools
        self.health = health or HealthRegistry()
        self.validate_references = validate
        self._agents: dict[str, AgentSpec] = {}
        for agent in agents or ():
            self.register(agent)

    # -- registration ----------------------------------------------------------

    def register(self, agent: AgentSpec, *, replace: bool = True, validate: bool | None = None) -> AgentSpec:
        if not isinstance(agent, AgentSpec):
            raise TypeError(f"expected an AgentSpec, got {type(agent).__name__}")
        if agent.id in self._agents and not replace:
            raise DuplicateRegistrationError(f"agent already registered: {agent.id}", agent=agent.id)
        should_validate = self.validate_references if validate is None else validate
        if should_validate:
            self.validate(agent)
        self._agents[agent.id] = agent
        return agent

    def register_all(self, agents: Iterable[AgentSpec], **kwargs: Any) -> None:
        for agent in agents:
            self.register(agent, **kwargs)

    def validate(self, agent: AgentSpec) -> None:
        problems = self.reference_problems(agent)
        if problems:
            raise InvalidReferenceError(
                f"agent '{agent.id}' has invalid references: " + "; ".join(problems),
                agent=agent.id,
                problems=problems,
            )

    def reference_problems(self, agent: AgentSpec) -> builtins.list[str]:
        """Return human-readable problems without raising (used by ``airvis doctor``)."""
        problems: builtins.list[str] = []
        if self.backends is not None and not self.backends.has(agent.backend_id):
            problems.append(f"unknown backend_id '{agent.backend_id}'")
        if agent.provider_id and self.providers is not None and not self.providers.has(agent.provider_id):
            problems.append(f"unknown provider_id '{agent.provider_id}'")
        if agent.model and agent.provider_id and self.providers is not None and self.providers.has(agent.provider_id):
            provider = self.providers.get(agent.provider_id)
            if not provider.supports_model(agent.model):
                problems.append(f"provider '{agent.provider_id}' does not serve model '{agent.model}'")
        if self.tools is not None:
            missing = sorted(name for name in agent.tools if not self.tools.has(name))
            if missing:
                problems.append(f"unknown tool(s): {', '.join(missing)}")
        return problems

    def unregister(self, agent_id: str) -> bool:
        return self._agents.pop(agent_id, None) is not None

    # -- lookup ----------------------------------------------------------------

    def get(self, agent_id: str) -> AgentSpec:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise UnknownAgentError(
                f"unknown agent: {agent_id}", agent=agent_id, known=sorted(self._agents)
            ) from exc

    def has(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def names(self) -> builtins.list[str]:
        return sorted(self._agents)

    def all(self, *, enabled_only: bool = True) -> builtins.list[AgentSpec]:
        agents = list(self._agents.values())
        if enabled_only:
            agents = [agent for agent in agents if agent.enabled]
        return agents

    def with_capabilities(self, capabilities: Iterable[str], *, partial: bool = False) -> builtins.list[AgentSpec]:
        wanted = set(capabilities)
        if not wanted:
            return self.all()
        if partial:
            return [agent for agent in self.all() if wanted & agent.capabilities]
        return [agent for agent in self.all() if agent.covers(wanted)]

    def by_role(self, role: str) -> builtins.list[AgentSpec]:
        return [agent for agent in self.all() if agent.role == role]

    def list(self) -> builtins.list[dict[str, Any]]:
        payload: builtins.list[dict[str, Any]] = []
        for agent in sorted(self._agents.values(), key=lambda item: item.id):
            entry = agent.to_dict()
            stats = self.health.stats(agent.id)
            entry["stats"] = {
                "active_tasks": stats.active_tasks,
                "successes": stats.successes,
                "failures": stats.failures,
                "reliability": round(stats.reliability, 4),
                "average_latency_ms": round(stats.average_latency_ms, 2),
            }
            entry["health"] = stats.health.to_dict()
            entry["reference_problems"] = self.reference_problems(agent)
            payload.append(entry)
        return payload

    def __iter__(self) -> Iterator[AgentSpec]:
        return iter(self._agents.values())

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._agents


__all__ = ["AgentRegistry"]
