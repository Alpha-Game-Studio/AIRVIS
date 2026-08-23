"""Concurrent DAG execution with cycle detection, cancellation and resume."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..core.config import WorkflowConfig
from ..core.errors import DAGCycleError, DAGError
from ..core.events import EventBus, EventType
from .task import Task, TaskResult, TaskStatus

log = logging.getLogger("airvis.dag")

TaskRunner = Callable[[Task], Awaitable[TaskResult]]


def validate_graph(tasks: Iterable[Task]) -> None:
    """Raise on unknown dependencies or cycles before anything executes."""
    catalogue = {task.id: task for task in tasks}
    for task in catalogue.values():
        unknown = [item for item in task.dependencies if item not in catalogue]
        if unknown:
            raise DAGError(
                f"task '{task.name}' depends on unknown task(s): {', '.join(unknown)}",
                task_id=task.id,
                unknown=unknown,
            )

    #: 0 = unvisited, 1 = on the current path, 2 = fully explored
    state: dict[str, int] = {task_id: 0 for task_id in catalogue}
    path: list[str] = []

    def visit(task_id: str) -> None:
        if state[task_id] == 1:
            cycle = [*path[path.index(task_id) :], task_id]
            raise DAGCycleError(
                "dependency cycle detected: " + " -> ".join(catalogue[item].name for item in cycle),
                cycle=cycle,
            )
        if state[task_id] == 2:
            return
        state[task_id] = 1
        path.append(task_id)
        for dependency in catalogue[task_id].dependencies:
            visit(dependency)
        path.pop()
        state[task_id] = 2

    for task_id in catalogue:
        visit(task_id)


def topological_layers(tasks: Iterable[Task]) -> list[list[str]]:
    """Group task ids into layers that may execute concurrently."""
    catalogue = {task.id: task for task in tasks}
    validate_graph(catalogue.values())
    remaining = dict(catalogue)
    done: set[str] = set()
    layers: list[list[str]] = []
    while remaining:
        layer = [
            task_id
            for task_id, task in remaining.items()
            if all(dependency in done for dependency in task.dependencies)
        ]
        if not layer:  # pragma: no cover - validate_graph already rejects cycles
            raise DAGCycleError("dependency cycle detected while layering")
        layers.append(sorted(layer))
        for task_id in layer:
            done.add(task_id)
            del remaining[task_id]
    return layers


@dataclass
class DAGRun:
    """Mutable execution state for one workflow run."""

    workflow_id: str
    tasks: dict[str, Task] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    cancelled: bool = False

    def order(self) -> list[Task]:
        return sorted(self.tasks.values(), key=lambda task: task.created_at)

    def dependents_of(self, task_id: str) -> list[Task]:
        return [task for task in self.tasks.values() if task_id in task.dependencies]

    def unfinished(self) -> list[Task]:
        return [task for task in self.tasks.values() if not task.status.terminal]


class DAGEngine:
    """Executes a task graph, running independent nodes concurrently."""

    def __init__(
        self,
        *,
        config: WorkflowConfig | None = None,
        event_bus: EventBus | None = None,
        store: Any = None,
    ) -> None:
        self.config = config or WorkflowConfig()
        self.event_bus = event_bus
        self.store = store
        self._runs: dict[str, DAGRun] = {}

    # -- inspection ------------------------------------------------------------

    def run_state(self, workflow_id: str) -> DAGRun | None:
        return self._runs.get(workflow_id)

    def cancel(self, workflow_id: str) -> bool:
        run = self._runs.get(workflow_id)
        if run is None:
            return False
        run.cancelled = True
        return True

    # -- execution -------------------------------------------------------------

    async def execute(
        self,
        tasks: Iterable[Task],
        runner: TaskRunner,
        *,
        workflow_id: str,
        resume: bool = False,
    ) -> DAGRun:
        catalogue = {task.id: task for task in tasks}
        if not catalogue:
            raise DAGError("cannot execute an empty task graph", workflow_id=workflow_id)
        validate_graph(catalogue.values())

        run = DAGRun(workflow_id=workflow_id, tasks=catalogue)
        self._runs[workflow_id] = run

        for task in catalogue.values():
            task.workflow_id = workflow_id
            if resume and task.status is TaskStatus.COMPLETED:
                continue
            if resume and task.status in {TaskStatus.RUNNING, TaskStatus.FAILED}:
                # An interrupted or previously failed node is retried on resume.
                task.status = TaskStatus.QUEUED
            elif not resume:
                task.status = TaskStatus.QUEUED
            self._publish(EventType.TASK_CREATED, task, status=task.status.value)

        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        running: dict[asyncio.Task[TaskResult], Task] = {}
        failed_hard = False

        try:
            while True:
                if run.cancelled:
                    self._cancel_remaining(run, "workflow cancelled")
                    break

                ready = self._ready(run)
                while ready and len(running) < max(1, self.config.max_concurrency):
                    task = ready.pop(0)
                    task.status = TaskStatus.RUNNING
                    handle = asyncio.create_task(
                        self._run_one(task, runner, semaphore), name=f"airvis-task-{task.id}"
                    )
                    running[handle] = task

                if not running:
                    if self._ready(run):
                        continue
                    break

                done, _ = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)
                for handle in done:
                    task = running.pop(handle)
                    try:
                        result = handle.result()
                    except asyncio.CancelledError:
                        task.status = TaskStatus.CANCELLED
                        task.finished_at = time.time()
                        self._publish(EventType.TASK_CANCELLED, task, status="cancelled")
                        continue
                    except Exception as exc:
                        result = TaskResult(
                            task_id=task.id,
                            ok=False,
                            error=str(exc),
                            error_code=getattr(exc, "code", type(exc).__name__),
                        )
                    self._settle(run, task, result)
                    if not result.ok:
                        if self.config.cancel_dependents_on_failure:
                            self._cancel_dependents(run, task)
                        if self.config.fail_fast:
                            failed_hard = True
                if failed_hard:
                    self._cancel_remaining(run, "fail_fast is enabled")
                    for handle in list(running):
                        handle.cancel()
                    if running:
                        await asyncio.gather(*running.keys(), return_exceptions=True)
                    break
        finally:
            for handle in list(running):
                handle.cancel()
            if running:
                await asyncio.gather(*running.keys(), return_exceptions=True)

        return run

    # -- internals -------------------------------------------------------------

    def _ready(self, run: DAGRun) -> list[Task]:
        ready: list[Task] = []
        for task in run.order():
            if task.status is not TaskStatus.QUEUED:
                continue
            dependencies = [run.tasks[item] for item in task.dependencies if item in run.tasks]
            if task.finalizer:
                # A finalizer waits for its dependencies to settle, however they settle.
                if any(not dependency.status.terminal for dependency in dependencies):
                    continue
            elif any(dependency.status is not TaskStatus.COMPLETED for dependency in dependencies):
                continue
            ready.append(task)
        ready.sort(key=lambda task: (-task.priority, task.created_at))
        return ready

    async def _run_one(self, task: Task, runner: TaskRunner, semaphore: asyncio.Semaphore) -> TaskResult:
        async with semaphore:
            task.mark_running()
            self._publish(EventType.TASK_STARTED, task, status="running")
            started = time.perf_counter()
            try:
                result = await runner(task)
            except Exception as exc:
                result = TaskResult(
                    task_id=task.id,
                    ok=False,
                    error=str(exc),
                    error_code=getattr(exc, "code", type(exc).__name__),
                )
            result.duration_ms = result.duration_ms or (time.perf_counter() - started) * 1000
            return result

    def _settle(self, run: DAGRun, task: Task, result: TaskResult) -> None:
        if result.ok:
            task.mark_completed(result)
            self._publish(EventType.TASK_COMPLETED, task, status="completed", duration_ms=result.duration_ms)
        else:
            task.mark_failed(result)
            self._publish(EventType.TASK_FAILED, task, status="failed", duration_ms=result.duration_ms)
        self._persist(task)

    def _cancel_dependents(self, run: DAGRun, failed: Task) -> None:
        queue = [failed.id]
        while queue:
            current = queue.pop()
            for dependent in run.dependents_of(current):
                if dependent.status.terminal or dependent.finalizer:
                    continue
                dependent.status = TaskStatus.CANCELLED
                dependent.finished_at = time.time()
                dependent.result = TaskResult(
                    task_id=dependent.id,
                    ok=False,
                    error=f"upstream task '{failed.name}' failed",
                    error_code="upstream_failed",
                )
                self._publish(EventType.TASK_CANCELLED, dependent, status="upstream_failed")
                self._persist(dependent)
                queue.append(dependent.id)

    def _cancel_remaining(self, run: DAGRun, reason: str) -> None:
        for task in run.unfinished():
            task.status = TaskStatus.CANCELLED
            task.finished_at = time.time()
            task.result = task.result or TaskResult(
                task_id=task.id, ok=False, error=reason, error_code="task_cancelled"
            )
            self._publish(EventType.TASK_CANCELLED, task, status="cancelled")
            self._persist(task)

    def _publish(self, event_type: EventType, task: Task, **fields: Any) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(
            event_type,
            workflow_id=task.workflow_id,
            task_id=task.id,
            agent_id=task.assigned_agent_id,
            metadata={"name": task.name, "attempts": task.attempts},
            **fields,
        )

    def _persist(self, task: Task) -> None:
        if self.store is None:
            return
        try:
            self.store.save_task(task.to_dict())
        except Exception:  # persistence must never break execution
            log.debug("failed to persist task %s", task.id, exc_info=True)


def add_tasks(run: DAGRun, tasks: Iterable[Task], *, rewire_dependents_of: str | None = None) -> list[Task]:
    """Inject tasks into a live run (used by the REPLAN repair strategy)."""
    added = list(tasks)
    for task in added:
        task.workflow_id = run.workflow_id
        run.tasks[task.id] = task
    if rewire_dependents_of and added:
        terminal_ids = [task.id for task in added if not any(task.id in other.dependencies for other in added)]
        for dependent in run.dependents_of(rewire_dependents_of):
            if dependent.id in {task.id for task in added}:
                continue
            dependent.dependencies = [
                item for item in dependent.dependencies if item != rewire_dependents_of
            ] + terminal_ids
            if dependent.status is TaskStatus.CANCELLED:
                dependent.status = TaskStatus.QUEUED
                dependent.result = None
    validate_graph(run.tasks.values())
    return added


__all__ = ["DAGEngine", "DAGRun", "TaskRunner", "add_tasks", "topological_layers", "validate_graph"]
