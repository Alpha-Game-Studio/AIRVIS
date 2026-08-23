"""Provider registry with health tracking and an explicit fallback chain."""

from __future__ import annotations

import builtins
import time
from collections.abc import Iterable, Iterator
from typing import Any

from ..core.errors import (
    DuplicateRegistrationError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    UnknownProviderError,
)
from ..core.events import EventBus, EventType
from ..core.health import HealthRegistry, HealthState
from .base import GenerationRequest, GenerationResult, Provider


class ProviderRegistry:
    """The single source of truth for which models AIRVIS can talk to."""

    def __init__(
        self,
        providers: Iterable[Provider] | None = None,
        *,
        health: HealthRegistry | None = None,
        event_bus: EventBus | None = None,
        fallbacks: builtins.list[str] | None = None,
    ) -> None:
        self._providers: dict[str, Provider] = {}
        self.health = health or HealthRegistry()
        self.event_bus = event_bus
        self.fallbacks: builtins.list[str] = list(fallbacks or [])
        for provider in providers or ():
            self.register(provider)

    # -- registration ----------------------------------------------------------

    def register(self, provider: Provider, *, replace: bool = True) -> Provider:
        if not isinstance(provider, Provider):
            raise TypeError(f"expected a Provider instance, got {type(provider).__name__}")
        if provider.id in self._providers and not replace:
            raise DuplicateRegistrationError(f"provider already registered: {provider.id}", provider=provider.id)
        self._providers[provider.id] = provider
        return provider

    def unregister(self, provider_id: str) -> bool:
        return self._providers.pop(provider_id, None) is not None

    def get(self, provider_id: str) -> Provider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise UnknownProviderError(
                f"unknown provider: {provider_id}", provider=provider_id, known=sorted(self._providers)
            ) from exc

    def has(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def names(self) -> builtins.list[str]:
        return sorted(self._providers)

    @property
    def default(self) -> Provider:
        if not self._providers:
            raise UnknownProviderError("no providers are registered")
        return next(iter(self._providers.values()))

    def list(self) -> builtins.list[dict[str, Any]]:
        payload: builtins.list[dict[str, Any]] = []
        for provider in self._providers.values():
            stats = self.health.stats(provider.id)
            entry = provider.describe()
            entry["health"] = stats.health.to_dict()
            entry["stats"] = {
                "active_tasks": stats.active_tasks,
                "successes": stats.successes,
                "failures": stats.failures,
                "average_latency_ms": round(stats.average_latency_ms, 2),
                "reliability": round(stats.reliability, 4),
            }
            entry["status"] = "ready" if self.health.is_usable(provider.id) else "unavailable"
            payload.append(entry)
        return payload

    def __iter__(self) -> Iterator[Provider]:
        return iter(self._providers.values())

    def __len__(self) -> int:
        return len(self._providers)

    def __contains__(self, provider_id: object) -> bool:
        return provider_id in self._providers

    # -- selection -------------------------------------------------------------

    def candidates(
        self,
        *,
        capability: str | None = None,
        local_only: bool = False,
        model: str = "",
        exclude: Iterable[str] = (),
        require_healthy: bool = True,
    ) -> builtins.list[Provider]:
        excluded = set(exclude)
        found: builtins.list[Provider] = []
        for provider in self._providers.values():
            if provider.id in excluded:
                continue
            if capability and not provider.capabilities.supports(capability):
                continue
            if local_only and not provider.local:
                continue
            if model and not provider.supports_model(model):
                continue
            if require_healthy and not self.health.is_usable(provider.id):
                continue
            found.append(provider)
        return found

    def chain(self, provider_id: str | None = None, extra_fallbacks: Iterable[str] = ()) -> builtins.list[Provider]:
        """Ordered attempt list: primary first, then configured fallbacks."""
        ordered: builtins.list[Provider] = []
        seen: set[str] = set()
        for candidate in [provider_id, *extra_fallbacks, *self.fallbacks]:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if self.has(candidate):
                ordered.append(self.get(candidate))
        if not ordered:
            usable = self.candidates(require_healthy=True) or list(self._providers.values())
            if not usable:
                raise UnknownProviderError("no providers are registered")
            ordered = usable[:1]
        for provider in self._providers.values():
            if provider.id not in seen and provider.local and provider not in ordered:
                ordered.append(provider)
                seen.add(provider.id)
        return ordered

    # -- execution -------------------------------------------------------------

    async def generate(
        self,
        request: GenerationRequest,
        *,
        provider_id: str | None = None,
        fallbacks: Iterable[str] = (),
        workflow_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
    ) -> GenerationResult:
        """Generate with automatic failover across the provider chain."""
        attempts = self.chain(provider_id, fallbacks)
        failures: builtins.list[str] = []
        for provider in attempts:
            self.health.acquire(provider.id)
            started = time.perf_counter()
            try:
                result = await provider.generate(request)
            except RateLimitError as exc:
                self.health.record_failure(provider.id, str(exc), rate_limited_for=30.0)
                failures.append(f"{provider.id}: {exc}")
                continue
            except ProviderError as exc:
                self.health.record_failure(provider.id, str(exc))
                failures.append(f"{provider.id}: {exc}")
                continue
            except Exception as exc:
                self.health.record_failure(provider.id, str(exc))
                failures.append(f"{provider.id}: {type(exc).__name__}: {exc}")
                continue
            else:
                latency = result.latency_ms or (time.perf_counter() - started) * 1000
                self.health.record_success(provider.id, latency)
                if self.event_bus is not None:
                    self.event_bus.publish(
                        EventType.PROVIDER_SELECTED,
                        provider_id=provider.id,
                        model=result.model,
                        workflow_id=workflow_id,
                        task_id=task_id,
                        agent_id=agent_id,
                        duration_ms=latency,
                        status="ok",
                        metadata={"usage": result.usage.to_dict()},
                    )
                return result
            finally:
                self.health.release(provider.id)

        raise ProviderUnavailableError(
            "every provider in the chain failed: " + "; ".join(failures),
            attempted=[provider.id for provider in attempts],
            failures=failures,
        )

    async def health_check_all(self) -> dict[str, dict[str, Any]]:
        report: dict[str, dict[str, Any]] = {}
        for provider in self._providers.values():
            try:
                status = await provider.health_check()
            except Exception as exc:  # a broken health check is itself unhealthy
                from ..core.health import HealthStatus

                status = HealthStatus(HealthState.UNHEALTHY, f"health check raised {type(exc).__name__}: {exc}",
                                      time.time())
            self.health.set_health(provider.id, status)
            report[provider.id] = status.to_dict()
        return report


__all__ = ["ProviderRegistry"]
