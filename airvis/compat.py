"""Adapters that keep the AIRVIS V4 public API working on the V6 engine.

Nothing here contains business logic: each class forwards to the canonical V6
implementation. New code should use :class:`airvis.engine.AirvisEngine`.
"""

from __future__ import annotations

import builtins
import warnings
from typing import Any

from .core.asyncutil import run_blocking
from .core.health import HealthState, HealthStatus
from .providers.base import (
    GenerationRequest,
    GenerationResult,
    Message,
    Provider,
    ProviderCapabilities,
)
from .providers.registry import ProviderRegistry


def deprecated(name: str, replacement: str) -> None:
    warnings.warn(
        f"{name} is deprecated and will be removed in AIRVIS 7; use {replacement} instead",
        DeprecationWarning,
        stacklevel=3,
    )


class LegacyProviderAdapter(Provider):
    """Wraps a V4 duck-typed provider (``chat(messages, tools) -> str``)."""

    capabilities = ProviderCapabilities(chat=True)

    def __init__(self, legacy: Any) -> None:
        self.legacy = legacy
        self.id = str(getattr(legacy, "id", "legacy"))
        self.default_model = str(getattr(legacy, "model", ""))
        self.local = bool(getattr(legacy, "local", False))
        declared = getattr(legacy, "capabilities", None)
        if isinstance(declared, (set, frozenset, list, tuple)):
            self.capabilities = ProviderCapabilities(
                chat=True,
                tool_calling="tools" in declared,
                vision="vision" in declared,
            )
            self.local = self.local or "local" in declared
        super().__init__()

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        messages = [message.to_dict() for message in request.messages]
        text = await _maybe_thread(self.legacy.chat, messages, request.tools)
        return GenerationResult(text=str(text), provider=self.id, model=self.resolve_model(request.model))

    async def health_check(self) -> HealthStatus:
        import time

        return HealthStatus(HealthState.UNKNOWN, "legacy provider adapter", time.time())


async def _maybe_thread(func: Any, *args: Any) -> Any:
    import asyncio
    import inspect

    if inspect.iscoroutinefunction(func):
        return await func(*args)
    return await asyncio.to_thread(func, *args)


class LegacyProviderManager:
    """``AgentRuntime.provider_manager`` shim backed by :class:`ProviderRegistry`."""

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

    @property
    def providers(self) -> builtins.list[Provider]:
        return list(self.registry)

    def add(self, provider: Any) -> None:
        self.registry.register(provider if isinstance(provider, Provider) else LegacyProviderAdapter(provider))

    def list(self) -> builtins.list[dict[str, Any]]:
        return self.registry.list()

    def chat(self, messages: builtins.list[dict[str, str]], tools: builtins.list[dict[str, Any]] | None = None) -> str:
        request = GenerationRequest(
            messages=[Message.coerce(item) for item in messages] or [Message("user", "")],
            tools=list(tools or []),
        )
        return run_blocking(self.registry.generate(request)).text


class LegacyModelRouter:
    """``AgentRuntime.router`` shim exposing the V4 ``status()``/``choose()`` API."""

    def __init__(self, config: Any, providers: ProviderRegistry) -> None:
        self.config = config
        self.providers = providers

    @property
    def provider_id(self) -> str:
        names = self.providers.names()
        return self.providers.default.id if names else ""

    @property
    def model(self) -> str:
        return self.providers.default.default_model if len(self.providers) else ""

    def choose(self, task: str = "general", requires_network: bool = False) -> dict[str, Any]:
        strategy = str(self.config.routing.strategy).lower()
        local_only = strategy == "local_only"
        candidates = self.providers.candidates(local_only=local_only) or list(self.providers)
        chosen = candidates[0] if candidates else None
        return {
            "provider": chosen.id if chosen else "",
            "model": chosen.default_model if chosen else "",
            "local": bool(chosen.local) if chosen else False,
        }

    def status(self) -> dict[str, Any]:
        return {
            "mode": str(self.config.routing.strategy).upper(),
            "strategy": self.config.routing.strategy,
            "weights": self.config.routing.resolved_weights(),
            "providers": self.providers.names(),
            "selected": self.choose(),
        }


class LegacyAgentDelegator:
    """``AgentRuntime.agents`` shim backed by the V6 :class:`AgentRegistry`."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    @property
    def agents(self) -> dict[str, Any]:
        return {agent.role: agent for agent in self.engine.agents.all()}

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": agent.id,
                "role": agent.role,
                "tools": sorted(agent.tools),
                "capabilities": sorted(agent.capabilities),
                "backend_id": agent.backend_id,
                "provider_id": agent.provider_id,
                "model": agent.model,
            }
            for agent in self.engine.agents.all()
        ]

    def delegate(self, role: str, prompt: str, runtime: Any = None) -> str:
        matches = self.engine.agents.by_role(role) or (
            [self.engine.agents.get(role)] if self.engine.agents.has(role) else []
        )
        if not matches:
            raise KeyError(f"Unknown agent role: {role}")
        from .orchestration.task import Task

        agent = matches[0]
        task = Task(description=prompt, name=f"delegate:{agent.id}")
        task.forced_agent_id = agent.id
        task.required_capabilities = []
        from .orchestration.task import Plan

        plan = Plan(request=prompt, tasks=[task], strategy="delegated")
        result = run_blocking(self.engine.orchestrator.run(prompt, plan=plan))
        return result.output


__all__ = [
    "LegacyAgentDelegator",
    "LegacyModelRouter",
    "LegacyProviderAdapter",
    "LegacyProviderManager",
    "deprecated",
]
