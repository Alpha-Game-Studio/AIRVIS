"""DAGEngine, ReviewSystem, RepairSystem, ContextManager and ArtifactManager."""

from __future__ import annotations

import asyncio

import pytest

from airvis.artifacts.manager import ArtifactManager, ArtifactType
from airvis.context.manager import ContextManager
from airvis.core.config import ContextConfig, RepairConfig, ReviewConfig, WorkflowConfig
from airvis.core.errors import (
    ArtifactError,
    BackendUnavailableError,
    DAGCycleError,
    DAGError,
    PermissionDeniedError,
    RateLimitError,
    ToolExecutionError,
)
from airvis.orchestration.dag import DAGEngine, add_tasks, topological_layers, validate_graph
from airvis.orchestration.repair import (
    ErrorAnalyzer,
    FailureAnalysis,
    FailureCategory,
    RepairPlanner,
    RepairStrategy,
)
from airvis.orchestration.review import ReviewSystem
from airvis.orchestration.task import RetryPolicy, Task, TaskResult, TaskStatus


def make_task(name: str, deps: list[str] | None = None, **kwargs) -> Task:
    return Task(description=f"do {name}", name=name, dependencies=list(deps or []), **kwargs)


class TestGraphValidation:
    def test_detects_a_two_node_cycle(self):
        first, second = make_task("a"), make_task("b")
        first.dependencies = [second.id]
        second.dependencies = [first.id]
        with pytest.raises(DAGCycleError):
            validate_graph([first, second])

    def test_detects_a_self_cycle(self):
        task = make_task("a")
        task.dependencies = [task.id]
        with pytest.raises(DAGCycleError):
            validate_graph([task])

    def test_detects_a_longer_cycle(self):
        a, b, c = make_task("a"), make_task("b"), make_task("c")
        a.dependencies, b.dependencies, c.dependencies = [c.id], [a.id], [b.id]
        with pytest.raises(DAGCycleError):
            validate_graph([a, b, c])

    def test_unknown_dependency_is_rejected(self):
        with pytest.raises(DAGError):
            validate_graph([make_task("a", ["does-not-exist"])])

    def test_layers_group_independent_nodes(self):
        a, b = make_task("a"), make_task("b")
        c = make_task("c", [a.id, b.id])
        layers = topological_layers([a, b, c])
        assert sorted(layers[0]) == sorted([a.id, b.id]) and layers[1] == [c.id]


class TestDAGEngine:
    async def test_runs_independent_tasks_concurrently(self, event_bus):
        engine = DAGEngine(config=WorkflowConfig(max_concurrency=4), event_bus=event_bus)
        a, b = make_task("a"), make_task("b")
        active, peak = 0, 0

        async def runner(task: Task) -> TaskResult:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return TaskResult(task_id=task.id, ok=True, output=task.name)

        await engine.execute([a, b], runner, workflow_id="w1")
        assert peak == 2

    async def test_respects_dependencies(self, event_bus):
        engine = DAGEngine(config=WorkflowConfig(max_concurrency=4), event_bus=event_bus)
        a = make_task("a")
        b = make_task("b", [a.id])
        order: list[str] = []

        async def runner(task: Task) -> TaskResult:
            order.append(task.name)
            return TaskResult(task_id=task.id, ok=True)

        await engine.execute([a, b], runner, workflow_id="w2")
        assert order == ["a", "b"]

    async def test_concurrency_limit_is_honoured(self):
        engine = DAGEngine(config=WorkflowConfig(max_concurrency=1))
        tasks = [make_task(f"t{index}") for index in range(4)]
        active, peak = 0, 0

        async def runner(task: Task) -> TaskResult:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return TaskResult(task_id=task.id, ok=True)

        await engine.execute(tasks, runner, workflow_id="w3")
        assert peak == 1

    async def test_failure_cancels_dependents(self):
        engine = DAGEngine(config=WorkflowConfig(cancel_dependents_on_failure=True))
        a = make_task("a")
        b = make_task("b", [a.id])
        c = make_task("c", [b.id])

        async def runner(task: Task) -> TaskResult:
            return TaskResult(task_id=task.id, ok=task.name != "a", error="boom" if task.name == "a" else None)

        run = await engine.execute([a, b, c], runner, workflow_id="w4")
        assert run.tasks[a.id].status is TaskStatus.FAILED
        assert run.tasks[b.id].status is TaskStatus.CANCELLED
        assert run.tasks[c.id].status is TaskStatus.CANCELLED

    async def test_independent_branch_survives_a_failure(self):
        engine = DAGEngine(config=WorkflowConfig())
        a, b = make_task("a"), make_task("b")

        async def runner(task: Task) -> TaskResult:
            return TaskResult(task_id=task.id, ok=task.name != "a", error="boom" if task.name == "a" else None)

        run = await engine.execute([a, b], runner, workflow_id="w5")
        assert run.tasks[b.id].status is TaskStatus.COMPLETED

    async def test_runner_exception_becomes_a_failed_task(self):
        engine = DAGEngine(config=WorkflowConfig())
        task = make_task("a")

        async def runner(_: Task) -> TaskResult:
            raise ToolExecutionError("kaboom", tool="x")

        run = await engine.execute([task], runner, workflow_id="w6")
        assert run.tasks[task.id].status is TaskStatus.FAILED
        assert run.tasks[task.id].result.error_code == "tool_execution_error"

    async def test_empty_graph_is_rejected(self):
        with pytest.raises(DAGError):
            await DAGEngine().execute([], lambda task: None, workflow_id="w7")

    async def test_cancellation_stops_the_run(self):
        engine = DAGEngine(config=WorkflowConfig(max_concurrency=1))
        tasks = [make_task(f"t{index}") for index in range(3)]

        async def runner(task: Task) -> TaskResult:
            engine.cancel("w8")
            return TaskResult(task_id=task.id, ok=True)

        run = await engine.execute(tasks, runner, workflow_id="w8")
        assert any(item.status is TaskStatus.CANCELLED for item in run.tasks.values())

    async def test_resume_skips_completed_tasks(self):
        engine = DAGEngine(config=WorkflowConfig())
        done = make_task("done")
        done.status = TaskStatus.COMPLETED
        done.result = TaskResult(task_id=done.id, ok=True, output="cached")
        pending = make_task("pending", [done.id])
        seen: list[str] = []

        async def runner(task: Task) -> TaskResult:
            seen.append(task.name)
            return TaskResult(task_id=task.id, ok=True)

        await engine.execute([done, pending], runner, workflow_id="w9", resume=True)
        assert seen == ["pending"]

    async def test_events_are_emitted(self, event_bus):
        engine = DAGEngine(config=WorkflowConfig(), event_bus=event_bus)
        task = make_task("a")

        async def runner(item: Task) -> TaskResult:
            return TaskResult(task_id=item.id, ok=True)

        await engine.execute([task], runner, workflow_id="w10")
        types = {event["type"] for event in event_bus.history(workflow_id="w10")}
        assert {"task.created", "task.started", "task.completed"} <= types

    def test_add_tasks_rewires_dependents(self):
        from airvis.orchestration.dag import DAGRun

        original = make_task("original")
        dependent = make_task("dependent", [original.id])
        run = DAGRun(workflow_id="w11", tasks={original.id: original, dependent.id: dependent})
        replacement = make_task("replacement")
        add_tasks(run, [replacement], rewire_dependents_of=original.id)
        assert dependent.dependencies == [replacement.id]


class TestReviewSystem:
    async def test_failed_task_is_rejected(self):
        review = ReviewSystem(ReviewConfig())
        task = make_task("a")
        result = TaskResult(task_id=task.id, ok=False, error="boom")
        assert (await review.review_task(task, result)).status == "FAIL"

    async def test_empty_output_is_rejected(self):
        review = ReviewSystem(ReviewConfig())
        task = make_task("a")
        result = TaskResult(task_id=task.id, ok=True, output="")
        assert (await review.review_task(task, result)).status == "FAIL"

    async def test_failing_tests_are_rejected(self):
        review = ReviewSystem(ReviewConfig())
        task = make_task("a", required_capabilities=["test"])
        result = TaskResult(
            task_id=task.id,
            ok=True,
            output="done",
            tool_results=[{"tool": "test.run", "ok": False, "error": "2 failed",
                           "metadata": {"failed_count": 2}}],
        )
        outcome = await review.review_task(task, result)
        assert outcome.status == "FAIL"
        assert any(issue.dimension in {"tests", "regressions"} for issue in outcome.blocking_issues())

    async def test_leaked_credential_is_rejected(self):
        review = ReviewSystem(ReviewConfig())
        task = make_task("a")
        result = TaskResult(task_id=task.id, ok=True, output='api_key = "AKIA1234567890ABCD"')
        assert (await review.review_task(task, result)).status == "FAIL"

    async def test_code_task_without_a_file_change_is_rejected(self):
        review = ReviewSystem(ReviewConfig())
        task = make_task("a", required_capabilities=["code"])
        result = TaskResult(task_id=task.id, ok=True, output="I thought about it")
        outcome = await review.review_task(task, result)
        assert any(issue.dimension == "requirements" for issue in outcome.issues)

    async def test_clean_result_passes(self):
        review = ReviewSystem(ReviewConfig())
        task = make_task("a")
        result = TaskResult(task_id=task.id, ok=True, output="all good")
        outcome = await review.review_task(task, result)
        assert outcome.status == "PASS" and outcome.score >= 0.9

    async def test_disabled_review_always_passes(self):
        review = ReviewSystem(ReviewConfig(enabled=False))
        task = make_task("a")
        result = TaskResult(task_id=task.id, ok=False, error="boom")
        assert (await review.review_task(task, result)).status == "PASS"

    async def test_recommendations_are_actionable(self):
        review = ReviewSystem(ReviewConfig())
        task = make_task("a", required_capabilities=["test"])
        result = TaskResult(
            task_id=task.id, ok=True, output="done",
            tool_results=[{"tool": "test.run", "ok": False, "error": "failed", "metadata": {"failed_count": 1}}],
        )
        assert (await review.review_task(task, result)).recommendations

    async def test_workflow_review_fails_when_a_task_failed(self):
        review = ReviewSystem(ReviewConfig())
        ok, bad = make_task("ok"), make_task("bad")
        ok.status = TaskStatus.COMPLETED
        bad.status = TaskStatus.FAILED
        bad.result = TaskResult(task_id=bad.id, ok=False, error="boom")
        assert (await review.review_workflow("req", [ok, bad])).status == "FAIL"


class TestErrorAnalyzer:
    @pytest.mark.parametrize(
        ("exc", "category"),
        [
            (RateLimitError("429"), FailureCategory.RATE_LIMIT),
            (BackendUnavailableError("down"), FailureCategory.BACKEND_ERROR),
            (PermissionDeniedError("nope"), FailureCategory.PERMISSION_ERROR),
            (ToolExecutionError("bad"), FailureCategory.TOOL_ERROR),
            (asyncio.TimeoutError(), FailureCategory.TIMEOUT),
            (ValueError("oops"), FailureCategory.CODE_ERROR),
            (RuntimeError("???"), FailureCategory.UNKNOWN),
        ],
    )
    def test_classifies_exceptions(self, exc, category):
        assert ErrorAnalyzer().classify_exception(exc).category is category

    def test_cancellation_is_not_retryable(self):
        analysis = ErrorAnalyzer().classify_exception(asyncio.CancelledError())
        assert analysis.category is FailureCategory.CANCELLED and not analysis.retryable

    def test_classifies_results_by_error_code(self):
        result = TaskResult(task_id="t", ok=False, error="x", error_code="rate_limit")
        assert ErrorAnalyzer().classify_result(result).category is FailureCategory.RATE_LIMIT

    def test_detects_a_test_failure_from_tool_results(self):
        result = TaskResult(
            task_id="t", ok=False, error="tests failed",
            tool_results=[{"tool": "test.run", "ok": False}],
        )
        assert ErrorAnalyzer().classify_result(result).category is FailureCategory.TEST_FAILURE


class TestRepairPlanner:
    def test_first_provider_error_is_retried(self):
        task = make_task("a")
        analysis = FailureAnalysis(FailureCategory.PROVIDER_ERROR, "boom")
        assert RepairPlanner(RepairConfig()).plan(task, analysis).strategy is RepairStrategy.RETRY

    def test_rate_limit_switches_provider_first(self):
        task = make_task("a")
        analysis = FailureAnalysis(FailureCategory.RATE_LIMIT, "429")
        assert RepairPlanner(RepairConfig()).plan(task, analysis).strategy is RepairStrategy.CHANGE_PROVIDER

    def test_strategies_escalate_and_never_repeat(self):
        planner = RepairPlanner(RepairConfig(max_repairs_per_task=10, max_retries=1))
        task = make_task("a", retry_policy=RetryPolicy(max_attempts=1))
        analysis = FailureAnalysis(FailureCategory.PROVIDER_ERROR, "boom")
        chosen: list[str] = []
        for _ in range(5):
            decision = planner.plan(task, analysis)
            chosen.append(decision.strategy.value)
            if decision.gives_up:
                break
            task.repair_attempts += 1
            task.attempted_repairs.append(decision.strategy.value)
        assert len(chosen) == len(set(chosen))
        assert chosen[-1] == RepairStrategy.ABORT.value

    def test_task_budget_is_enforced(self):
        planner = RepairPlanner(RepairConfig(max_repairs_per_task=1))
        task = make_task("a")
        task.repair_attempts = 1
        analysis = FailureAnalysis(FailureCategory.UNKNOWN, "boom")
        assert planner.plan(task, analysis).strategy is RepairStrategy.ABORT

    def test_workflow_budget_is_enforced(self):
        planner = RepairPlanner(RepairConfig(max_repairs_per_workflow=2))
        analysis = FailureAnalysis(FailureCategory.UNKNOWN, "boom")
        decision = planner.plan(make_task("a"), analysis, workflow_repairs=2)
        assert decision.strategy is RepairStrategy.ABORT

    def test_non_retryable_failure_aborts_immediately(self):
        analysis = FailureAnalysis(FailureCategory.CANCELLED, "stopped", retryable=False)
        assert RepairPlanner().plan(make_task("a"), analysis).strategy is RepairStrategy.ABORT

    def test_approval_strategy_needs_a_handler(self):
        planner = RepairPlanner(RepairConfig())
        analysis = FailureAnalysis(FailureCategory.PERMISSION_ERROR, "denied")
        without = planner.plan(make_task("a"), analysis, has_approval_handler=False)
        with_handler = planner.plan(make_task("a"), analysis, has_approval_handler=True)
        assert without.strategy is not RepairStrategy.REQUEST_APPROVAL
        assert with_handler.strategy is RepairStrategy.REQUEST_APPROVAL

    def test_playbook_is_configurable(self):
        planner = RepairPlanner(RepairConfig(strategies={"TOOL_ERROR": ["CHANGE_BACKEND"]}))
        analysis = FailureAnalysis(FailureCategory.TOOL_ERROR, "bad")
        assert planner.plan(make_task("a"), analysis).strategy is RepairStrategy.CHANGE_BACKEND

    def test_every_playbook_terminates(self):
        planner = RepairPlanner(RepairConfig())
        for category in FailureCategory:
            assert planner.playbook(category)[-1] is RepairStrategy.ABORT, category


class TestArtifactManager:
    def test_creates_and_reads_back(self, artifacts: ArtifactManager):
        artifact = artifacts.create("report", "summary", content="hello", task_id="t1", workflow_id="w1")
        assert artifacts.read(artifact.id) == "hello"
        assert artifact.digest and artifact.size == 5

    def test_serialises_structured_content(self, artifacts: ArtifactManager):
        artifact = artifacts.create(ArtifactType.JSON, "payload", content={"a": 1})
        assert artifact.size > 0

    def test_versions_are_chained(self, artifacts: ArtifactManager):
        first = artifacts.create("report", "summary", content="v1")
        second = artifacts.new_version(first.id, "v2")
        assert second.version == 2 and second.parent_id == first.id

    def test_latest_only_hides_superseded_versions(self, artifacts: ArtifactManager):
        first = artifacts.create("report", "summary", content="v1", workflow_id="w")
        artifacts.new_version(first.id, "v2")
        assert [item.version for item in artifacts.list(workflow_id="w", latest_only=True)] == [2]

    def test_unknown_artifact_raises(self, artifacts: ArtifactManager):
        with pytest.raises(ArtifactError):
            artifacts.get("nope")

    def test_reference_is_compact(self, artifacts: ArtifactManager):
        artifact = artifacts.create("file", "x.py", content="print(1)")
        assert set(artifact.reference()) == {"id", "type", "name", "path", "version", "size", "task_id"}

    def test_large_content_spills_to_disk(self, artifacts: ArtifactManager):
        artifact = artifacts.create("log", "big", content="x" * 70_000, workflow_id="w")
        assert artifact.path is not None and artifact.content is None
        assert artifacts.read(artifact.id).startswith("x")

    def test_from_tool_result_registers_descriptors(self, artifacts: ArtifactManager):
        created = artifacts.from_tool_result(
            [{"type": "patch", "name": "fix.patch", "content": "diff"}], task_id="t", workflow_id="w"
        )
        assert len(created) == 1 and created[0].type is ArtifactType.PATCH


class TestContextManager:
    def test_builds_a_bundle_with_upstream_results(self, context: ContextManager):
        upstream = TaskResult(task_id="t0", ok=True, output="upstream output")
        bundle = context.build(make_task("a"), request="원 요청", upstream_results=[upstream])
        rendered = "\n".join(message.content for message in bundle.to_messages())
        assert "upstream output" in rendered and "원 요청" in rendered

    def test_compresses_oversized_context(self, artifacts, workspace):
        manager = ContextManager(ContextConfig(max_chars=1200), artifacts=artifacts, workspace=workspace)
        upstream = [TaskResult(task_id=f"t{i}", ok=True, output="x" * 5000) for i in range(6)]
        bundle = manager.build(make_task("a"), request="req", upstream_results=upstream)
        assert bundle.metadata.get("compressed") is True
        assert bundle.size() < 12_000

    def test_no_compression_mode_keeps_everything(self, artifacts, workspace):
        manager = ContextManager(
            ContextConfig(max_chars=100, compression="none"), artifacts=artifacts, workspace=workspace
        )
        upstream = [TaskResult(task_id="t", ok=True, output="y" * 5000)]
        bundle = manager.build(make_task("a"), request="req", upstream_results=upstream)
        assert bundle.metadata.get("compressed") is None

    def test_workflow_messages_are_bounded(self, context: ContextManager):
        context.start_workflow("w", "request")
        for index in range(100):
            context.record_message("w", "assistant", f"message {index}")
        assert len(context.workflow_messages("w")) <= context.config.max_messages

    def test_artifacts_are_referenced_not_inlined(self, context: ContextManager, artifacts):
        artifacts.create("log", "huge", content="z" * 5000, workflow_id="w")
        bundle = context.build(make_task("a"), request="req", workflow_id="w")
        rendered = "\n".join(message.content for message in bundle.to_messages())
        assert "huge" in rendered and "z" * 5000 not in rendered
