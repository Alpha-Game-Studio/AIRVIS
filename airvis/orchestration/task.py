"""Task and workflow data model shared by the planner, DAG and orchestrator."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    QUEUED = "queued"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}


class WorkflowStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0

    def delay_for(self, attempt: int) -> float:
        delay = self.backoff_seconds * (self.backoff_multiplier ** max(0, attempt - 1))
        return min(delay, self.max_backoff_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
            "backoff_multiplier": self.backoff_multiplier,
            "max_backoff_seconds": self.max_backoff_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RetryPolicy:
        data = data or {}
        return cls(
            max_attempts=int(data.get("max_attempts", 3)),
            backoff_seconds=float(data.get("backoff_seconds", 0.5)),
            backoff_multiplier=float(data.get("backoff_multiplier", 2.0)),
            max_backoff_seconds=float(data.get("max_backoff_seconds", 30.0)),
        )


@dataclass
class ToolStep:
    """A concrete tool invocation the planner attached to a task.

    Directed steps make offline execution real: the backend runs them through
    the tool registry instead of hoping a model emits the right call.
    """

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": self.arguments, "optional": self.optional}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolStep:
        return cls(
            tool=str(data.get("tool", "")),
            arguments=dict(data.get("arguments") or {}),
            optional=bool(data.get("optional", False)),
        )


@dataclass
class TaskResult:
    """Outcome of a single task execution attempt."""

    task_id: str
    ok: bool = True
    output: str = ""
    error: str | None = None
    error_code: str | None = None
    agent_id: str | None = None
    backend_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
    duration_ms: float = 0.0
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "error_code": self.error_code,
            "agent_id": self.agent_id,
            "backend_id": self.backend_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "duration_ms": round(self.duration_ms, 2),
            "tool_results": self.tool_results,
            "artifact_ids": self.artifact_ids,
            "usage": self.usage,
            "metadata": self.metadata,
        }


@dataclass
class Task:
    """A unit of work produced by the planner and executed by the DAG engine."""

    description: str = ""
    #: V4 alias — ``Task(prompt="...")`` keeps working and stays in sync with
    #: ``description`` after construction.
    prompt: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    workflow_id: str | None = None
    required_capabilities: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    tool_plan: list[ToolStep] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    #: a finalizer runs once its dependencies reach *any* terminal state, and is
    #: never cancelled by an upstream failure — the "always report" node.
    finalizer: bool = False
    priority: float = 1.0
    timeout: float = 300.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    assigned_agent_id: str | None = None
    status: TaskStatus = TaskStatus.QUEUED
    attempts: int = 0
    repair_attempts: int = 0
    result: TaskResult | None = None
    review: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: strategies already tried by the repair system, to avoid loops
    attempted_repairs: list[str] = field(default_factory=list)
    #: routing overrides injected by repair strategies
    forced_agent_id: str | None = None
    excluded_agent_ids: list[str] = field(default_factory=list)
    excluded_provider_ids: list[str] = field(default_factory=list)
    excluded_backend_ids: list[str] = field(default_factory=list)
    override_provider_id: str | None = None
    override_model: str | None = None
    override_backend_id: str | None = None

    def __post_init__(self) -> None:
        self.description = (self.description or self.prompt).strip()
        self.prompt = self.description
        if not self.description:
            raise ValueError("Task requires a description")
        if not self.name:
            self.name = self.description[:60] or self.id
        if isinstance(self.retry_policy, dict):  # tolerated for hand-written plans
            self.retry_policy = RetryPolicy.from_dict(self.retry_policy)
        self.tool_plan = [ToolStep.from_dict(item) if isinstance(item, dict) else item for item in self.tool_plan]

    # -- convenience -----------------------------------------------------------

    @property
    def retry_count(self) -> int:
        """V4 alias for the number of completed attempts."""
        return max(0, self.attempts - 1)

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING
        self.attempts += 1
        self.started_at = time.time()

    def mark_completed(self, result: TaskResult) -> None:
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.finished_at = time.time()

    def mark_failed(self, result: TaskResult) -> None:
        self.status = TaskStatus.FAILED
        self.result = result
        self.finished_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "workflow_id": self.workflow_id,
            "required_capabilities": list(self.required_capabilities),
            "required_tools": list(self.required_tools),
            "tool_plan": [step.to_dict() for step in self.tool_plan],
            "dependencies": list(self.dependencies),
            "finalizer": self.finalizer,
            "priority": self.priority,
            "timeout": self.timeout,
            "retry_policy": self.retry_policy.to_dict(),
            "assigned_agent_id": self.assigned_agent_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "repair_attempts": self.repair_attempts,
            "attempted_repairs": list(self.attempted_repairs),
            "result": self.result.to_dict() if self.result else None,
            "review": self.review,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        task = cls(
            description=str(data.get("description") or data.get("prompt") or ""),
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=str(data.get("name") or ""),
            workflow_id=data.get("workflow_id"),
            required_capabilities=list(data.get("required_capabilities") or []),
            required_tools=list(data.get("required_tools") or []),
            tool_plan=[ToolStep.from_dict(item) for item in data.get("tool_plan") or []],
            dependencies=list(data.get("dependencies") or []),
            finalizer=bool(data.get("finalizer", False)),
            priority=float(data.get("priority", 1.0)),
            timeout=float(data.get("timeout", 300.0)),
            retry_policy=RetryPolicy.from_dict(data.get("retry_policy")),
            assigned_agent_id=data.get("assigned_agent_id"),
            status=TaskStatus(data.get("status", "queued")),
            attempts=int(data.get("attempts", 0)),
            repair_attempts=int(data.get("repair_attempts", 0)),
            attempted_repairs=list(data.get("attempted_repairs") or []),
            review=data.get("review"),
            created_at=float(data.get("created_at", time.time())),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            metadata=dict(data.get("metadata") or {}),
        )
        raw_result = data.get("result")
        if isinstance(raw_result, dict):
            task.result = TaskResult(
                task_id=str(raw_result.get("task_id", task.id)),
                ok=bool(raw_result.get("ok", True)),
                output=str(raw_result.get("output", "")),
                error=raw_result.get("error"),
                error_code=raw_result.get("error_code"),
                agent_id=raw_result.get("agent_id"),
                backend_id=raw_result.get("backend_id"),
                provider_id=raw_result.get("provider_id"),
                model=raw_result.get("model"),
                duration_ms=float(raw_result.get("duration_ms", 0.0)),
                tool_results=list(raw_result.get("tool_results") or []),
                artifact_ids=list(raw_result.get("artifact_ids") or []),
                usage=dict(raw_result.get("usage") or {}),
                metadata=dict(raw_result.get("metadata") or {}),
            )
        return task


@dataclass
class Plan:
    """An ordered set of tasks plus the request that produced it."""

    request: str
    tasks: list[Task] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    strategy: str = "heuristic"
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def task_by_id(self, task_id: str) -> Task | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request": self.request,
            "strategy": self.strategy,
            "created_at": self.created_at,
            "tasks": [task.to_dict() for task in self.tasks],
            "metadata": self.metadata,
        }


@dataclass
class WorkflowResult:
    """Everything the orchestrator produced for one user request."""

    workflow_id: str
    request: str
    status: WorkflowStatus
    output: str = ""
    tasks: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    repairs: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is WorkflowStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "request": self.request,
            "status": self.status.value,
            "ok": self.ok,
            "output": self.output,
            "tasks": self.tasks,
            "artifacts": self.artifacts,
            "reviews": self.reviews,
            "repairs": self.repairs,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "metadata": self.metadata,
        }


__all__ = [
    "Plan",
    "RetryPolicy",
    "Task",
    "TaskResult",
    "TaskStatus",
    "ToolStep",
    "WorkflowResult",
    "WorkflowStatus",
]
