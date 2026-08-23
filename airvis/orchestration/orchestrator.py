"""The orchestration pipeline.

    request -> planner -> DAG -> router -> agent -> backend -> provider -> tool
            -> artifact/context -> review -> (repair) -> result

Every arrow in that chain is a real call in :meth:`Orchestrator.run`; nothing
here is a placeholder that merely records an intention.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from ..agents.registry import AgentRegistry
from ..agents.router import AgentRouter
from ..artifacts.manager import ArtifactManager
from ..backends.base import ExecutionRequest, ExecutionResult
from ..backends.registry import BackendRegistry
from ..context.manager import ContextManager
from ..core.config import AirvisConfig
from ..core.errors import AirvisError, WorkflowCancelledError
from ..core.events import EventBus, EventType
from ..core.health import HealthRegistry
from ..providers.registry import ProviderRegistry
from ..security.permissions import PermissionManager
from ..tools.registry import ToolRegistry
from .dag import DAGEngine, add_tasks
from .planner import Planner
from .repair import ErrorAnalyzer, RepairDecision, RepairPlanner, RepairStrategy
from .review import ReviewResult, ReviewSystem
from .task import Plan, Task, TaskResult, TaskStatus, WorkflowResult, WorkflowStatus


class Orchestrator:
    """Connects planning, routing, execution, review and repair."""

    def __init__(
        self,
        *,
        config: AirvisConfig,
        agents: AgentRegistry,
        router: AgentRouter,
        backends: BackendRegistry,
        providers: ProviderRegistry,
        tools: ToolRegistry,
        permissions: PermissionManager,
        planner: Planner,
        dag: DAGEngine,
        review: ReviewSystem,
        context: ContextManager,
        artifacts: ArtifactManager,
        event_bus: EventBus,
        health: HealthRegistry,
        store: Any = None,
        analyzer: ErrorAnalyzer | None = None,
        repair_planner: RepairPlanner | None = None,
        approval_handler: Any = None,
    ) -> None:
        self.config = config
        self.agents = agents
        self.router = router
        self.backends = backends
        self.providers = providers
        self.tools = tools
        self.permissions = permissions
        self.planner = planner
        self.dag = dag
        self.review = review
        self.context = context
        self.artifacts = artifacts
        self.event_bus = event_bus
        self.health = health
        self.store = store
        self.analyzer = analyzer or ErrorAnalyzer()
        self.repair_planner = repair_planner or RepairPlanner(config.repair)
        self.approval_handler = approval_handler
        self._cancelled: set[str] = set()
        self._repairs: dict[str, list[dict[str, Any]]] = {}
        self._reviews: dict[str, list[dict[str, Any]]] = {}

    # -- public API ------------------------------------------------------------

    async def run(
        self,
        request: str,
        *,
        workflow_id: str | None = None,
        strategy: str | None = None,
        approval_handler: Any = None,
        plan: Plan | None = None,
    ) -> WorkflowResult:
        started = time.perf_counter()
        workflow_id = workflow_id or uuid.uuid4().hex[:12]
        handler = approval_handler or self.approval_handler
        self._repairs[workflow_id] = []
        self._reviews[workflow_id] = []
        self._cancelled.discard(workflow_id)

        self.event_bus.publish(
            EventType.WORKFLOW_CREATED, workflow_id=workflow_id, status="created",
            metadata={"request": request[:500]},
        )
        self._persist_workflow(workflow_id, request, WorkflowStatus.CREATED)
        self.context.start_workflow(workflow_id, request)

        try:
            plan = plan or await self.planner.plan(request, workflow_id=workflow_id)
        except Exception as exc:
            return self._failure(workflow_id, request, exc, started)

        for task in plan.tasks:
            task.workflow_id = workflow_id
        self.event_bus.publish(
            EventType.PLAN_CREATED, workflow_id=workflow_id, status="created",
            metadata={"tasks": len(plan.tasks), "strategy": plan.strategy},
        )
        self.event_bus.publish(EventType.WORKFLOW_STARTED, workflow_id=workflow_id, status="running")
        self._persist_workflow(workflow_id, request, WorkflowStatus.RUNNING, plan=plan)

        try:
            run = await self.dag.execute(
                plan.tasks,
                self._make_runner(request, workflow_id, strategy, handler),
                workflow_id=workflow_id,
            )
        except Exception as exc:
            return self._failure(workflow_id, request, exc, started, tasks=plan.tasks)

        tasks = run.order()
        workflow_review = await self.review.review_workflow(request, tasks, workflow_id=workflow_id)
        self._reviews[workflow_id].append(workflow_review.to_dict())
        if self.store is not None:
            self.store.save_review(workflow_id, workflow_review.to_dict())

        cancelled = workflow_id in self._cancelled
        status = (
            WorkflowStatus.CANCELLED if cancelled
            else WorkflowStatus.COMPLETED if workflow_review.passed
            else WorkflowStatus.FAILED
        )
        duration_ms = (time.perf_counter() - started) * 1000
        # Emit the terminal event before snapshotting history so the returned
        # result contains the complete event trail for this workflow.
        self.event_bus.publish(
            EventType.WORKFLOW_COMPLETED if status is WorkflowStatus.COMPLETED else EventType.WORKFLOW_FAILED,
            workflow_id=workflow_id,
            status=status.value,
            duration_ms=duration_ms,
        )
        result = WorkflowResult(
            workflow_id=workflow_id,
            request=request,
            status=status,
            output=self._compose_output(tasks, workflow_review),
            tasks=[task.to_dict() for task in tasks],
            artifacts=[artifact.to_dict() for artifact in self.artifacts.list(workflow_id=workflow_id)],
            reviews=self._reviews.get(workflow_id, []),
            repairs=self._repairs.get(workflow_id, []),
            events=self.event_bus.history(workflow_id=workflow_id, limit=400),
            error=None if status is WorkflowStatus.COMPLETED else workflow_review.issues[0].message
            if workflow_review.issues else None,
            duration_ms=duration_ms,
            metadata={"plan_strategy": plan.strategy, "task_count": len(tasks)},
        )
        self._persist_result(result)
        self.context.end_workflow(workflow_id)
        return result

    async def resume(self, workflow_id: str) -> WorkflowResult:
        """Re-run an interrupted workflow from its persisted task state."""
        if self.store is None:
            raise AirvisError("persistence is disabled; cannot resume", workflow_id=workflow_id)
        record = self.store.load_workflow(workflow_id)
        if record is None:
            raise AirvisError(f"unknown workflow: {workflow_id}", workflow_id=workflow_id)
        stored_tasks = self.store.load_tasks(workflow_id)
        if not stored_tasks:
            raise AirvisError(f"workflow '{workflow_id}' has no persisted tasks", workflow_id=workflow_id)
        request = str(record.get("request", ""))
        plan = Plan(request=request, tasks=[Task.from_dict(item) for item in stored_tasks], strategy="resumed")
        started = time.perf_counter()
        self._repairs.setdefault(workflow_id, [])
        self._reviews.setdefault(workflow_id, [])
        self.context.start_workflow(workflow_id, request)
        run = await self.dag.execute(
            plan.tasks,
            self._make_runner(request, workflow_id, None, self.approval_handler),
            workflow_id=workflow_id,
            resume=True,
        )
        tasks = run.order()
        workflow_review = await self.review.review_workflow(request, tasks, workflow_id=workflow_id)
        status = WorkflowStatus.COMPLETED if workflow_review.passed else WorkflowStatus.FAILED
        result = WorkflowResult(
            workflow_id=workflow_id,
            request=request,
            status=status,
            output=self._compose_output(tasks, workflow_review),
            tasks=[task.to_dict() for task in tasks],
            artifacts=[artifact.to_dict() for artifact in self.artifacts.list(workflow_id=workflow_id)],
            reviews=self._reviews.get(workflow_id, []),
            repairs=self._repairs.get(workflow_id, []),
            duration_ms=(time.perf_counter() - started) * 1000,
            metadata={"resumed": True},
        )
        self._persist_result(result)
        return result

    def cancel(self, workflow_id: str) -> bool:
        self._cancelled.add(workflow_id)
        self.event_bus.publish(EventType.WORKFLOW_CANCELLED, workflow_id=workflow_id, status="cancelled")
        return self.dag.cancel(workflow_id)

    def status(self, workflow_id: str) -> dict[str, Any]:
        run = self.dag.run_state(workflow_id)
        if run is None:
            record = self.store.load_workflow(workflow_id) if self.store is not None else None
            return record or {"workflow_id": workflow_id, "status": "unknown"}
        tasks = run.order()
        return {
            "workflow_id": workflow_id,
            "status": "cancelled" if workflow_id in self._cancelled else "running"
            if any(not task.status.terminal for task in tasks) else "finished",
            "tasks": [
                {
                    "id": task.id,
                    "name": task.name,
                    "status": task.status.value,
                    "agent_id": task.assigned_agent_id,
                    "attempts": task.attempts,
                    "repair_attempts": task.repair_attempts,
                }
                for task in tasks
            ],
        }

    # -- task execution --------------------------------------------------------

    def _make_runner(self, request: str, workflow_id: str, strategy: str | None, approval_handler: Any):
        async def runner(task: Task) -> TaskResult:
            return await self._execute_task(task, request, workflow_id, strategy, approval_handler)

        return runner

    async def _execute_task(
        self,
        task: Task,
        request: str,
        workflow_id: str,
        strategy: str | None,
        approval_handler: Any,
    ) -> TaskResult:
        review_notes: list[str] = []
        #: compact record of every attempt, so a later routing failure never
        #: erases the evidence collected by an earlier execution
        attempts: list[dict[str, Any]] = []

        while True:
            if workflow_id in self._cancelled:
                raise WorkflowCancelledError("workflow cancelled", workflow_id=workflow_id)

            try:
                result, review = await self._attempt(
                    task, request, workflow_id, strategy, approval_handler, review_notes
                )
            except Exception as exc:
                analysis = self.analyzer.classify_exception(exc)
                result = TaskResult(
                    task_id=task.id, ok=False, error=str(exc), error_code=analysis.code or analysis.category.value
                )
                review = None
            else:
                if result.ok and (review is None or review.passed):
                    if attempts:
                        result.metadata["previous_attempts"] = attempts
                    return result
                analysis = (
                    self.analyzer.classify_review(review)
                    if review is not None and not review.passed
                    else self.analyzer.classify_result(result)
                )

            attempts.append(
                {
                    "attempt": len(attempts) + 1,
                    "agent_id": result.agent_id,
                    "backend_id": result.backend_id,
                    "provider_id": result.provider_id,
                    "error": result.error,
                    "error_code": result.error_code,
                    "tool_results": result.tool_results,
                    "review": review.to_dict() if review is not None else None,
                }
            )

            decision = self.repair_planner.plan(
                task,
                analysis,
                workflow_repairs=len(self._repairs.get(workflow_id, [])),
                has_approval_handler=approval_handler is not None,
            )
            self.event_bus.publish(
                EventType.REPAIR_STARTED,
                workflow_id=workflow_id,
                task_id=task.id,
                agent_id=task.assigned_agent_id,
                status=decision.strategy.value,
                metadata=decision.analysis.to_dict(),
            )
            self._record_repair(workflow_id, task, decision)

            if decision.gives_up:
                self.event_bus.publish(
                    EventType.REPAIR_ABORTED, workflow_id=workflow_id, task_id=task.id,
                    status=decision.strategy.value, metadata={"reason": decision.reason},
                )
                result.metadata["repair"] = decision.to_dict()
                if len(attempts) > 1:
                    result.metadata["previous_attempts"] = attempts[:-1]
                if review is not None and not review.passed:
                    result.ok = False
                    result.error = result.error or "review rejected the output"
                    result.error_code = result.error_code or "review_rejected"
                    result.metadata["review"] = review.to_dict()
                return result

            task.repair_attempts += 1
            task.attempted_repairs.append(decision.strategy.value)
            applied = await self._apply_repair(
                task, decision, workflow_id, review_notes, review, request
            )
            self.event_bus.publish(
                EventType.REPAIR_COMPLETED, workflow_id=workflow_id, task_id=task.id,
                status=decision.strategy.value, metadata={"applied": applied},
            )
            if applied is not None and isinstance(applied, TaskResult):
                return applied
            if decision.delay_seconds > 0:
                await asyncio.sleep(decision.delay_seconds)

    async def _attempt(
        self,
        task: Task,
        request: str,
        workflow_id: str,
        strategy: str | None,
        approval_handler: Any,
        review_notes: list[str],
    ) -> tuple[TaskResult, ReviewResult | None]:
        started = time.perf_counter()

        # 1. agent selection
        decision = self.router.select(
            task,
            strategy=strategy,
            exclude=set(task.excluded_agent_ids),
            workflow_id=workflow_id,
        )
        agent = decision.agent
        task.assigned_agent_id = agent.id
        self.event_bus.publish(
            EventType.TASK_ASSIGNED, workflow_id=workflow_id, task_id=task.id, agent_id=agent.id,
            backend_id=agent.backend_id, provider_id=agent.provider_id, model=agent.model, status="assigned",
        )

        # 2. backend and provider resolution (explicit references, never inferred)
        backend_id = task.override_backend_id or agent.backend_id
        provider_id = task.override_provider_id or agent.provider_id
        model = task.override_model or agent.model

        # 3. context assembly
        bundle = self.context.build(
            task,
            request=request,
            workflow_id=workflow_id,
            agent=agent,
            upstream_results=self._upstream_results(task, workflow_id),
            review_notes=review_notes,
        )

        # 4. backend execution (which drives provider + tools)
        execution = ExecutionRequest(
            agent=agent,
            instruction=task.description,
            context=bundle,
            task_id=task.id,
            workflow_id=workflow_id,
            allowed_tools=agent.tools,
            tool_plan=list(task.tool_plan),
            provider_id=provider_id,
            model=model,
            timeout=min(task.timeout, self.config.workflow.task_timeout),
            max_iterations=agent.max_iterations,
            approval_handler=approval_handler,
        )
        # Backends are expected to honour ``request.timeout`` themselves; this
        # guard only covers backends that do not. The grace period is a small
        # fraction of the budget so a short timeout stays a short timeout.
        guard = execution.timeout + min(30.0, max(1.0, execution.timeout * 0.1))
        self.health.acquire(agent.id)
        try:
            execution_result: ExecutionResult = await asyncio.wait_for(
                self.backends.execute(backend_id, execution, exclude=set(task.excluded_backend_ids)),
                timeout=guard,
            )
        finally:
            self.health.release(agent.id)

        # 5. artifacts and context update
        artifacts = self.artifacts.from_tool_result(
            execution_result.artifacts, creator=agent.id, task_id=task.id, workflow_id=workflow_id
        )
        if self.store is not None:
            for artifact in artifacts:
                self.store.save_artifact(artifact.to_dict())
        self.context.record_message(workflow_id, "assistant", f"[{task.name}] {execution_result.output[:2000]}")

        result = TaskResult(
            task_id=task.id,
            ok=execution_result.ok,
            output=execution_result.output,
            error=execution_result.error,
            error_code=execution_result.error_code,
            agent_id=agent.id,
            backend_id=backend_id,
            provider_id=execution_result.provider_id or provider_id,
            model=execution_result.model or model,
            duration_ms=(time.perf_counter() - started) * 1000,
            tool_results=execution_result.tool_results,
            artifact_ids=[artifact.id for artifact in artifacts],
            usage=execution_result.usage,
            metadata={"task_name": task.name, "iterations": execution_result.iterations,
                      "routing": {"score": round(decision.score, 4), "strategy": decision.strategy}},
        )
        if result.ok:
            self.health.record_success(agent.id, result.duration_ms)
        else:
            self.health.record_failure(agent.id, result.error or "task failed")

        # 6. review gate
        review = await self.review.review_task(task, result, workflow_id=workflow_id, request=request)
        task.review = review.to_dict()
        self._reviews.setdefault(workflow_id, []).append(review.to_dict())
        if self.store is not None:
            self.store.save_review(workflow_id, {**review.to_dict(), "task_id": task.id})
        return result, review

    # -- repair application ----------------------------------------------------

    async def _apply_repair(
        self,
        task: Task,
        decision: RepairDecision,
        workflow_id: str,
        review_notes: list[str],
        review: ReviewResult | None,
        request: str,
    ) -> Any:
        strategy = decision.strategy

        if strategy is RepairStrategy.RETRY:
            return None

        if strategy is RepairStrategy.CHANGE_AGENT:
            if task.assigned_agent_id:
                task.excluded_agent_ids.append(task.assigned_agent_id)
            task.forced_agent_id = None
            return None

        if strategy is RepairStrategy.CHANGE_PROVIDER:
            current = task.override_provider_id or (
                self.agents.get(task.assigned_agent_id).provider_id if task.assigned_agent_id else None
            )
            if current:
                task.excluded_provider_ids.append(current)
            alternatives = [
                provider.id
                for provider in self.providers.candidates(exclude=set(task.excluded_provider_ids))
            ]
            task.override_provider_id = alternatives[0] if alternatives else None
            task.override_model = None
            return None

        if strategy is RepairStrategy.CHANGE_MODEL:
            provider_id = task.override_provider_id or (
                self.agents.get(task.assigned_agent_id).provider_id if task.assigned_agent_id else None
            )
            if provider_id and self.providers.has(provider_id):
                provider = self.providers.get(provider_id)
                options = [item for item in provider.models if item != (task.override_model or provider.default_model)]
                task.override_model = options[0] if options else None
            return None

        if strategy is RepairStrategy.CHANGE_BACKEND:
            current = task.override_backend_id or (
                self.agents.get(task.assigned_agent_id).backend_id if task.assigned_agent_id else None
            )
            if current:
                task.excluded_backend_ids.append(current)
            alternatives = [name for name in self.backends.names() if name not in task.excluded_backend_ids]
            task.override_backend_id = alternatives[0] if alternatives else None
            return None

        if strategy is RepairStrategy.MODIFY_CONTEXT:
            if review is not None:
                review_notes.extend(review.recommendations or [issue.message for issue in review.blocking_issues()])
            review_notes.append(f"직전 실패 사유: {decision.analysis.detail}")
            del review_notes[:-6]
            return None

        if strategy is RepairStrategy.REQUEST_APPROVAL:
            # The next attempt runs with the approval handler already attached;
            # granting the missing permission is what unblocks it.
            self.permissions.grant(*_missing_permissions(decision))
            return None

        if strategy is RepairStrategy.REPLAN:
            run = self.dag.run_state(workflow_id)
            if run is None:
                return None
            subtasks = await self.planner.replan(task, decision.analysis.detail, workflow_id=workflow_id)
            if not subtasks:
                return None
            add_tasks(run, subtasks, rewire_dependents_of=task.id)
            self.event_bus.publish(
                EventType.PLAN_REVISED, workflow_id=workflow_id, task_id=task.id, status="replanned",
                metadata={"subtasks": [item.id for item in subtasks]},
            )
            return TaskResult(
                task_id=task.id,
                ok=True,
                output=f"작업을 {len(subtasks)}개의 하위 작업으로 재계획했습니다.",
                agent_id=task.assigned_agent_id,
                metadata={"replanned_into": [item.id for item in subtasks], "task_name": task.name},
            )

        return None

    # -- helpers ---------------------------------------------------------------

    def _upstream_results(self, task: Task, workflow_id: str) -> list[TaskResult]:
        run = self.dag.run_state(workflow_id)
        if run is None:
            return []
        results: list[TaskResult] = []
        for dependency_id in task.dependencies:
            dependency = run.tasks.get(dependency_id)
            if dependency is not None and dependency.result is not None:
                results.append(dependency.result)
        return results

    def _compose_output(self, tasks: list[Task], review: ReviewResult) -> str:
        reporting = [
            task.result
            for task in tasks
            if task.status is TaskStatus.COMPLETED
            and "report" in task.required_capabilities
            and task.result is not None
        ]
        if reporting and (reporting[-1].output or "").strip():
            return reporting[-1].output

        # A single-task workflow (a plain question) answers directly.
        if len(tasks) == 1 and tasks[0].result is not None and (tasks[0].result.output or "").strip():
            return tasks[0].result.output

        lines: list[str] = []
        for task in tasks:
            if task.result is None:
                lines.append(f"- [{task.status.value}] {task.name}")
                continue
            marker = "✔" if task.status is TaskStatus.COMPLETED else "✘"
            body = (task.result.output or task.result.error or "").strip().splitlines()
            head = body[0][:400] if body else ""
            lines.append(f"{marker} {task.name}: {head}")
        if not review.passed and review.issues:
            lines.append("")
            lines.append("리뷰 반려 사유: " + "; ".join(issue.message for issue in review.blocking_issues()[:3]))
        return "\n".join(lines)

    def _record_repair(self, workflow_id: str, task: Task, decision: RepairDecision) -> None:
        payload = {**decision.to_dict(), "task_id": task.id, "task_name": task.name}
        self._repairs.setdefault(workflow_id, []).append(payload)
        if self.store is not None:
            self.store.save_repair(workflow_id, task.id, payload)

    def _failure(
        self, workflow_id: str, request: str, exc: BaseException, started: float, tasks: list[Task] | None = None
    ) -> WorkflowResult:
        analysis = self.analyzer.classify_exception(exc)
        result = WorkflowResult(
            workflow_id=workflow_id,
            request=request,
            status=WorkflowStatus.FAILED,
            output="",
            tasks=[task.to_dict() for task in tasks or []],
            reviews=self._reviews.get(workflow_id, []),
            repairs=self._repairs.get(workflow_id, []),
            error=str(exc),
            duration_ms=(time.perf_counter() - started) * 1000,
            metadata={"failure": analysis.to_dict()},
        )
        self.event_bus.publish(
            EventType.WORKFLOW_FAILED, workflow_id=workflow_id, status="failed",
            metadata={"error": str(exc), "category": analysis.category.value},
        )
        self._persist_result(result)
        self.context.end_workflow(workflow_id)
        return result

    def _persist_workflow(
        self, workflow_id: str, request: str, status: WorkflowStatus, *, plan: Plan | None = None
    ) -> None:
        if self.store is None:
            return
        payload: dict[str, Any] = {"workflow_id": workflow_id, "request": request, "status": status.value}
        if plan is not None:
            payload["plan"] = plan.to_dict()
        self.store.save_workflow(payload)
        if plan is not None:
            for task in plan.tasks:
                self.store.save_task(task.to_dict())

    def _persist_result(self, result: WorkflowResult) -> None:
        if self.store is None:
            return
        payload = result.to_dict()
        payload.pop("events", None)
        self.store.save_workflow(payload)


def _missing_permissions(decision: RepairDecision) -> list[str]:
    detail = decision.analysis.detail
    marker = "missing permission(s): "
    if marker in detail:
        return [item.strip() for item in detail.split(marker, 1)[1].split(",") if item.strip()]
    return []


__all__ = ["Orchestrator"]
