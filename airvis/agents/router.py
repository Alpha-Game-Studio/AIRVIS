"""Capability / cost / health / policy aware agent selection.

The router is the only place that decides *who* runs a task. It scores every
registered agent against the task and the routing strategy; nothing is
hardcoded and no mapping is inferred from identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.config import RoutingConfig, RoutingStrategy
from ..core.errors import NoAgentAvailableError, UnknownAgentError
from ..core.events import EventBus, EventType
from ..core.health import HealthRegistry
from .registry import AgentRegistry
from .spec import AgentSpec

MAX_EXPECTED_COST = 20.0  # USD / million tokens used to normalise the cost term
MAX_EXPECTED_LATENCY_MS = 60_000.0


@dataclass
class RoutingCandidate:
    agent: AgentSpec
    score: float
    components: dict[str, float] = field(default_factory=dict)
    rejected: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent.id,
            "score": round(self.score, 4),
            "components": {key: round(value, 4) for key, value in self.components.items()},
            "rejected": self.rejected,
            "backend_id": self.agent.backend_id,
            "provider_id": self.agent.provider_id,
            "model": self.agent.model,
        }


@dataclass
class RoutingDecision:
    agent: AgentSpec
    score: float
    strategy: str
    candidates: list[RoutingCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent.id,
            "score": round(self.score, 4),
            "strategy": self.strategy,
            "backend_id": self.agent.backend_id,
            "provider_id": self.agent.provider_id,
            "model": self.agent.model,
            "candidates": [item.to_dict() for item in self.candidates],
        }


class AgentRouter:
    """Scores and selects agents for tasks."""

    def __init__(
        self,
        agents: AgentRegistry,
        *,
        config: RoutingConfig | None = None,
        providers: Any = None,
        backends: Any = None,
        tools: Any = None,
        health: HealthRegistry | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.agents = agents
        self.config = config or RoutingConfig()
        self.providers = providers
        self.backends = backends
        self.tools = tools
        self.health = health or agents.health
        self.event_bus = event_bus

    # -- public API ------------------------------------------------------------

    def select(
        self,
        task: Any,
        *,
        strategy: str | None = None,
        exclude: set[str] | frozenset[str] | None = None,
        workflow_id: str | None = None,
    ) -> RoutingDecision:
        """Choose the best agent for ``task`` or raise :class:`NoAgentAvailableError`."""
        forced = getattr(task, "forced_agent_id", None)
        if forced:
            if not self.agents.has(forced):
                raise UnknownAgentError(f"task pins unknown agent '{forced}'", agent=forced)
            agent = self.agents.get(forced)
            decision = RoutingDecision(agent=agent, score=float("inf"), strategy="pinned")
            self._emit(decision, task, workflow_id)
            return decision

        excluded = set(exclude or ())
        excluded.update(getattr(task, "excluded_agent_ids", []) or [])
        resolved_strategy = self._strategy(strategy)
        weights = self._weights(resolved_strategy)

        candidates = [
            self._score(agent, task, weights, resolved_strategy, excluded)
            for agent in self.agents.all(enabled_only=True)
        ]
        viable = [item for item in candidates if item.rejected is None and item.score >= self.config.min_score]
        if not viable:
            reasons = "; ".join(
                f"{item.agent.id}: {item.rejected}" for item in candidates if item.rejected
            ) or "no agents registered"
            raise NoAgentAvailableError(
                f"no agent can serve task '{getattr(task, 'name', task)}': {reasons}",
                task_id=getattr(task, "id", None),
                required_capabilities=list(getattr(task, "required_capabilities", []) or []),
                candidates=[item.to_dict() for item in candidates],
            )

        viable.sort(key=lambda item: (-item.score, item.agent.id))
        best = viable[0]
        decision = RoutingDecision(
            agent=best.agent,
            score=best.score,
            strategy=resolved_strategy.value,
            candidates=sorted(candidates, key=lambda item: -item.score)[:8],
        )
        self._emit(decision, task, workflow_id)
        return decision

    def rank(self, task: Any, *, strategy: str | None = None) -> list[RoutingCandidate]:
        """Full scored ranking, for CLI inspection and debugging."""
        resolved = self._strategy(strategy)
        weights = self._weights(resolved)
        candidates = [self._score(agent, task, weights, resolved, set()) for agent in self.agents.all()]
        return sorted(candidates, key=lambda item: -item.score)

    # -- scoring ---------------------------------------------------------------

    def _score(
        self,
        agent: AgentSpec,
        task: Any,
        weights: dict[str, float],
        strategy: RoutingStrategy,
        excluded: set[str],
    ) -> RoutingCandidate:
        required_capabilities = list(getattr(task, "required_capabilities", []) or [])
        required_tools = set(getattr(task, "required_tools", []) or [])
        excluded_backends = set(getattr(task, "excluded_backend_ids", []) or [])
        excluded_providers = set(getattr(task, "excluded_provider_ids", []) or [])
        # Repair strategies can redirect a task to another backend/provider; the
        # agent must then be scored against that override, not its own default.
        effective_backend_id = getattr(task, "override_backend_id", None) or agent.backend_id
        effective_provider_id = getattr(task, "override_provider_id", None) or agent.provider_id

        candidate = RoutingCandidate(agent=agent, score=0.0)

        if agent.id in excluded:
            candidate.rejected = "excluded by the caller or a repair strategy"
            return candidate
        if required_capabilities and not agent.covers(required_capabilities):
            missing = sorted(set(required_capabilities) - agent.capabilities)
            candidate.rejected = f"missing capability: {', '.join(missing)}"
            return candidate
        if required_tools and not required_tools.issubset(agent.tools):
            missing = sorted(required_tools - agent.tools)
            candidate.rejected = f"missing tool access: {', '.join(missing)}"
            return candidate
        if effective_backend_id in excluded_backends:
            candidate.rejected = f"backend '{effective_backend_id}' excluded for this task"
            return candidate
        if effective_provider_id and effective_provider_id in excluded_providers:
            candidate.rejected = f"provider '{effective_provider_id}' excluded for this task"
            return candidate

        backend = self._backend(effective_backend_id)
        if self.backends is not None and backend is None:
            candidate.rejected = f"backend '{effective_backend_id}' is not registered"
            return candidate
        if backend is not None and not self.health.is_usable(effective_backend_id) and self.config.skip_unhealthy:
            candidate.rejected = f"backend '{effective_backend_id}' is unhealthy"
            return candidate

        provider = self._provider(effective_provider_id)
        if effective_provider_id and self.providers is not None and provider is None:
            candidate.rejected = f"provider '{effective_provider_id}' is not registered"
            return candidate
        if provider is not None and self.config.skip_unhealthy and not self.health.is_usable(provider.id):
            candidate.rejected = f"provider '{provider.id}' is unhealthy or rate limited"
            return candidate
        if strategy is RoutingStrategy.LOCAL_ONLY and provider is not None and not provider.local:
            candidate.rejected = "LOCAL_ONLY strategy rejects remote providers"
            return candidate
        effective_model = getattr(task, "override_model", None) or agent.model
        if effective_model and provider is not None and not provider.supports_model(effective_model):
            candidate.rejected = f"provider '{provider.id}' does not serve model '{effective_model}'"
            return candidate

        agent_stats = self.health.stats(agent.id)
        if agent.max_concurrency and agent_stats.active_tasks >= agent.max_concurrency:
            candidate.rejected = f"agent is saturated ({agent_stats.active_tasks}/{agent.max_concurrency})"
            return candidate

        provider_stats = self.health.stats(provider.id) if provider is not None else agent_stats
        cost = 0.0
        if provider is not None:
            cost = (provider.cost_per_million_input + provider.cost_per_million_output) / 2.0
        quality = provider.quality if provider is not None else agent.quality
        local = 1.0 if (provider is None or provider.local) else 0.0
        latency = provider_stats.average_latency_ms or agent_stats.average_latency_ms
        workload = agent_stats.active_tasks / max(1, agent.max_concurrency)
        health_score = 1.0 if self.health.is_usable(agent.id) else 0.0

        components = {
            "capability": agent.capability_match(required_capabilities),
            "reliability": (agent_stats.reliability + provider_stats.reliability) / 2.0,
            "health": health_score,
            "priority": min(2.0, agent.priority * float(getattr(task, "priority", 1.0))) / 2.0,
            "quality": max(0.0, min(1.0, quality)),
            "locality": local,
            "cost": min(1.0, cost / MAX_EXPECTED_COST),
            "latency": min(1.0, latency / MAX_EXPECTED_LATENCY_MS),
            "workload": min(1.0, workload),
        }

        score = (
            weights["capability"] * components["capability"]
            + weights["reliability"] * components["reliability"]
            + weights["health"] * components["health"]
            + weights["priority"] * components["priority"]
            + weights["quality"] * components["quality"]
            + weights["locality"] * components["locality"]
            - weights["cost"] * components["cost"]
            - weights["latency"] * components["latency"]
            - weights["workload"] * components["workload"]
        )
        candidate.score = score
        candidate.components = components
        return candidate

    # -- helpers ---------------------------------------------------------------

    def _strategy(self, strategy: str | None) -> RoutingStrategy:
        token = (strategy or self.config.strategy or RoutingStrategy.BALANCED.value)
        try:
            return RoutingStrategy(str(token).lower())
        except ValueError:
            return RoutingStrategy.BALANCED

    def _weights(self, strategy: RoutingStrategy) -> dict[str, float]:
        base = RoutingConfig(strategy=strategy.value, weights=self.config.weights)
        return base.resolved_weights()

    def _backend(self, backend_id: str) -> Any:
        if self.backends is None or not self.backends.has(backend_id):
            return None
        return self.backends.get(backend_id)

    def _provider(self, provider_id: str | None) -> Any:
        if self.providers is None or not provider_id or not self.providers.has(provider_id):
            return None
        return self.providers.get(provider_id)

    def _emit(self, decision: RoutingDecision, task: Any, workflow_id: str | None) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(
            EventType.AGENT_SELECTED,
            workflow_id=workflow_id,
            task_id=getattr(task, "id", None),
            agent_id=decision.agent.id,
            backend_id=decision.agent.backend_id,
            provider_id=decision.agent.provider_id,
            model=decision.agent.model,
            status="selected",
            metadata={"score": round(decision.score, 4), "strategy": decision.strategy},
        )


__all__ = ["AgentRouter", "RoutingCandidate", "RoutingDecision"]
