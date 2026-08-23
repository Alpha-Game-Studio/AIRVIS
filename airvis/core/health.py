"""Health and workload tracking for agents, backends and providers."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HealthState(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class HealthStatus:
    state: HealthState = HealthState.UNKNOWN
    detail: str = ""
    checked_at: float = 0.0
    latency_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.state in {HealthState.HEALTHY, HealthState.DEGRADED, HealthState.UNKNOWN}

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "latency_ms": self.latency_ms,
        }


@dataclass
class ResourceStats:
    """Rolling execution statistics for one routable resource."""

    id: str
    active_tasks: int = 0
    queued_tasks: int = 0
    successes: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    samples: int = 0
    last_error: str | None = None
    last_used: float = 0.0
    rate_limited_until: float = 0.0
    health: HealthStatus = field(default_factory=HealthStatus)

    @property
    def average_latency_ms(self) -> float:
        return self.total_latency_ms / self.samples if self.samples else 0.0

    @property
    def failure_rate(self) -> float:
        total = self.successes + self.failures
        return self.failures / total if total else 0.0

    @property
    def reliability(self) -> float:
        """Laplace-smoothed success ratio in ``[0, 1]``; unknowns start optimistic."""
        return (self.successes + 1.0) / (self.successes + self.failures + 2.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "active_tasks": self.active_tasks,
            "queued_tasks": self.queued_tasks,
            "successes": self.successes,
            "failures": self.failures,
            "failure_rate": round(self.failure_rate, 4),
            "reliability": round(self.reliability, 4),
            "average_latency_ms": round(self.average_latency_ms, 2),
            "last_error": self.last_error,
            "last_used": self.last_used,
            "rate_limited": self.rate_limited,
            "health": self.health.to_dict(),
        }

    @property
    def rate_limited(self) -> bool:
        return time.time() < self.rate_limited_until


class HealthRegistry:
    """Thread-safe workload/health book-keeping shared by every router."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stats: dict[str, ResourceStats] = {}

    def stats(self, resource_id: str) -> ResourceStats:
        with self._lock:
            if resource_id not in self._stats:
                self._stats[resource_id] = ResourceStats(id=resource_id)
            return self._stats[resource_id]

    def acquire(self, resource_id: str) -> None:
        with self._lock:
            stats = self.stats(resource_id)
            stats.active_tasks += 1
            stats.last_used = time.time()

    def release(self, resource_id: str) -> None:
        with self._lock:
            stats = self.stats(resource_id)
            stats.active_tasks = max(0, stats.active_tasks - 1)

    def record_success(self, resource_id: str, latency_ms: float) -> None:
        with self._lock:
            stats = self.stats(resource_id)
            stats.successes += 1
            stats.total_latency_ms += max(0.0, latency_ms)
            stats.samples += 1
            stats.last_error = None
            if stats.health.state in {HealthState.UNKNOWN, HealthState.DEGRADED}:
                stats.health = HealthStatus(HealthState.HEALTHY, "recovered", time.time())

    def record_failure(self, resource_id: str, error: str, *, rate_limited_for: float = 0.0) -> None:
        with self._lock:
            stats = self.stats(resource_id)
            stats.failures += 1
            stats.last_error = error
            if rate_limited_for > 0:
                stats.rate_limited_until = time.time() + rate_limited_for
            if stats.failure_rate >= 0.75 and stats.failures >= 3:
                stats.health = HealthStatus(HealthState.UNHEALTHY, error, time.time())
            elif stats.failure_rate >= 0.4:
                stats.health = HealthStatus(HealthState.DEGRADED, error, time.time())

    def set_health(self, resource_id: str, status: HealthStatus) -> None:
        with self._lock:
            self.stats(resource_id).health = status

    def is_usable(self, resource_id: str) -> bool:
        with self._lock:
            stats = self.stats(resource_id)
            return stats.health.state is not HealthState.UNHEALTHY and not stats.rate_limited

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [stats.to_dict() for stats in self._stats.values()]

    def reset(self) -> None:
        with self._lock:
            self._stats.clear()


__all__ = ["HealthRegistry", "HealthState", "HealthStatus", "ResourceStats"]
