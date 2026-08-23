"""Structured observability events for the orchestration pipeline."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("airvis.events")


class EventType(str, Enum):
    WORKFLOW_CREATED = "workflow.created"
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    WORKFLOW_CANCELLED = "workflow.cancelled"

    PLAN_CREATED = "plan.created"
    PLAN_REVISED = "plan.revised"

    TASK_CREATED = "task.created"
    TASK_ASSIGNED = "task.assigned"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    TASK_SKIPPED = "task.skipped"

    AGENT_SELECTED = "agent.selected"
    BACKEND_SELECTED = "backend.selected"
    PROVIDER_SELECTED = "provider.selected"

    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_DENIED = "tool.denied"
    APPROVAL_REQUESTED = "approval.requested"

    ARTIFACT_CREATED = "artifact.created"

    REVIEW_STARTED = "review.started"
    REVIEW_COMPLETED = "review.completed"

    REPAIR_STARTED = "repair.started"
    REPAIR_COMPLETED = "repair.completed"
    REPAIR_ABORTED = "repair.aborted"

    HEALTH_CHANGED = "health.changed"


@dataclass(slots=True)
class Event:
    """A single structured record emitted by the pipeline."""

    type: EventType
    timestamp: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    workflow_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    backend_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
    tool: str | None = None
    duration_ms: float | None = None
    status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "timestamp": self.timestamp,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "backend_id": self.backend_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "tool": self.tool,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "metadata": self.metadata,
        }


Handler = Callable[[Event], None]


class EventBus:
    """Thread-safe synchronous fan-out bus.

    Handlers are plain callables; a misbehaving handler is logged and never
    breaks the pipeline (observability must not be able to fail execution).
    """

    def __init__(self, keep_last: int = 500) -> None:
        self._lock = threading.RLock()
        self._handlers: list[tuple[Handler, frozenset[EventType] | None]] = []
        self._history: list[Event] = []
        self._keep_last = max(0, keep_last)

    def subscribe(self, handler: Handler, types: Iterable[EventType] | None = None) -> Callable[[], None]:
        selector = frozenset(types) if types is not None else None
        with self._lock:
            self._handlers.append((handler, selector))

        def unsubscribe() -> None:
            with self._lock:
                self._handlers[:] = [item for item in self._handlers if item[0] is not handler]

        return unsubscribe

    def emit(self, event: Event) -> Event:
        with self._lock:
            if self._keep_last:
                self._history.append(event)
                if len(self._history) > self._keep_last:
                    del self._history[: len(self._history) - self._keep_last]
            handlers = list(self._handlers)
        for handler, selector in handlers:
            if selector is not None and event.type not in selector:
                continue
            try:
                handler(event)
            except Exception:  # pragma: no cover - defensive
                log.exception("event handler failed for %s", event.type.value)
        return event

    def publish(self, type: EventType, **fields: Any) -> Event:
        return self.emit(Event(type=type, **fields))

    def history(self, workflow_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._history)
        if workflow_id:
            events = [event for event in events if event.workflow_id == workflow_id]
        return [event.to_dict() for event in events[-limit:]]

    def clear(self) -> None:
        with self._lock:
            self._history.clear()


def logging_handler(level: int = logging.INFO) -> Handler:
    """Return a handler that renders events into the standard logging module."""

    def handle(event: Event) -> None:
        log.log(
            level,
            "%s workflow=%s task=%s agent=%s backend=%s provider=%s status=%s",
            event.type.value,
            event.workflow_id,
            event.task_id,
            event.agent_id,
            event.backend_id,
            event.provider_id,
            event.status,
        )

    return handle


__all__ = ["Event", "EventBus", "EventType", "Handler", "logging_handler"]
