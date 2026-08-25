"""Backend registry and router."""

from __future__ import annotations

import builtins
import time
from collections.abc import Iterable, Iterator
from typing import Any

from ..core.errors import (
    BackendUnavailableError,
    DuplicateRegistrationError,
    ProviderError,
    UnknownBackendError,
)
from ..core.events import EventBus, EventType
from ..core.health import HealthRegistry, HealthState, HealthStatus
from .base import Backend, ExecutionRequest, ExecutionResult


class BackendRegistry:
    """Holds backends, tracks their health and routes executions to them."""

    def __init__(
        self,
        backends: Iterable[Backend] | None = None,
        *,
        health: HealthRegistry | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._backends: dict[str, Backend] = {}
        self.health = health or HealthRegistry()
        self.event_bus = event_bus
        for backend in backends or ():
            self.register(backend)

    def register(self, backend: Backend, *, replace: bool = True) -> Backend:
        if not isinstance(backend, Backend):
            raise TypeError(f"expected a Backend instance, got {type(backend).__name__}")
        if backend.id in self._backends and not replace:
            raise DuplicateRegistrationError(f"backend already registered: {backend.id}", backend=backend.id)
        self._backends[backend.id] = backend
        return backend

    def unregister(self, backend_id: str) -> bool:
        return self._backends.pop(backend_id, None) is not None

    def get(self, backend_id: str) -> Backend:
        try:
            return self._backends[backend_id]
        except KeyError as exc:
            raise UnknownBackendError(
                f"unknown backend: {backend_id}", backend=backend_id, known=sorted(self._backends)
            ) from exc

    def has(self, backend_id: str) -> bool:
        return backend_id in self._backends

    def names(self) -> builtins.list[str]:
        return sorted(self._backends)

    def list(self) -> builtins.list[dict[str, Any]]:
        payload: builtins.list[dict[str, Any]] = []
        for backend in sorted(self._backends.values(), key=lambda item: item.id):
            stats = self.health.stats(backend.id)
            entry = backend.describe()
            entry["health"] = stats.health.to_dict()
            entry["stats"] = {
                "active_tasks": stats.active_tasks,
                "successes": stats.successes,
                "failures": stats.failures,
                "reliability": round(stats.reliability, 4),
                "average_latency_ms": round(stats.average_latency_ms, 2),
            }
            entry["status"] = "ready" if self.health.is_usable(backend.id) else "unavailable"
            payload.append(entry)
        return payload

    def __iter__(self) -> Iterator[Backend]:
        return iter(self._backends.values())

    def __len__(self) -> int:
        return len(self._backends)

    def __contains__(self, backend_id: object) -> bool:
        return backend_id in self._backends

    def resolve(self, backend_id: str, *, exclude: Iterable[str] = ()) -> Backend:
        """Return the requested backend, honouring per-task exclusions."""
        excluded = set(exclude)
        if backend_id not in excluded and self.has(backend_id):
            backend = self.get(backend_id)
            if self.health.is_usable(backend_id):
                return backend
            raise BackendUnavailableError(
                f"backend '{backend_id}' is unhealthy", backend=backend_id,
                detail=self.health.stats(backend_id).health.detail,
            )
        if backend_id in excluded:
            raise BackendUnavailableError(
                f"backend '{backend_id}' is excluded for this task", backend=backend_id
            )
        raise UnknownBackendError(f"unknown backend: {backend_id}", backend=backend_id, known=self.names())

    async def execute(
        self, backend_id: str, request: ExecutionRequest, *, exclude: Iterable[str] = ()
    ) -> ExecutionResult:
        """Execute through ``backend_id`` while recording backend health.

        Provider failures are deliberately *not* recorded as backend failures.
        Native is an orchestration backend that hosts providers; a provider
        outage must not poison the native backend and make every subsequent
        task unroutable. ProviderRegistry owns provider health and failover.
        """
        backend = self.resolve(backend_id, exclude=exclude)
        if self.event_bus is not None:
            self.event_bus.publish(
                EventType.BACKEND_SELECTED,
                workflow_id=request.workflow_id,
                task_id=request.task_id,
                agent_id=request.agent.id,
                backend_id=backend.id,
                provider_id=request.provider_id or request.agent.provider_id,
                model=request.model or request.agent.model,
                status="selected",
            )
        self.health.acquire(backend.id)
        started = time.perf_counter()
        try:
            result = await backend.execute(request)
        except ProviderError:
            # ProviderRegistry has already recorded the provider failure and
            # decides whether another provider can be used. Do not turn that
            # failure into ``native unhealthy``.
            raise
        except Exception as exc:
            self.health.record_failure(backend.id, str(exc))
            raise
        else:
            self.health.record_success(backend.id, result.duration_ms or (time.perf_counter() - started) * 1000)
            return result
        finally:
            self.health.release(backend.id)

    async def cancel(self, execution_id: str) -> bool:
        cancelled = False
        for backend in self._backends.values():
            if await backend.cancel(execution_id):
                cancelled = True
        return cancelled

    async def health_check_all(self) -> dict[str, dict[str, Any]]:
        report: dict[str, dict[str, Any]] = {}
        for backend in self._backends.values():
            try:
                status = await backend.health_check()
            except Exception as exc:
                status = HealthStatus(
                    HealthState.UNHEALTHY, f"health check raised {type(exc).__name__}: {exc}", time.time()
                )
            self.health.set_health(backend.id, status)
            report[backend.id] = status.to_dict()
        return report

    async def close(self) -> None:
        for backend in self._backends.values():
            await backend.close()


BackendRouter = BackendRegistry


__all__ = ["BackendRegistry", "BackendRouter"]
