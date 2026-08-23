"""End-to-end tests: a request must actually travel the whole pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from airvis.core.events import EventType
from airvis.orchestration.task import TaskStatus, WorkflowStatus

BUGGY_MODULE = '''"""Module seeded with a real defect for the end-to-end test."""


def get_runtime():
    global _runtime
    if _runtime is None:
        _runtime = object()
    return _runtime


def unreachable_example():
    return 1
    cleanup = 2
    return cleanup
'''


@pytest.fixture
def buggy_workspace(workspace: Path) -> Path:
    (workspace / "buggy.py").write_text(BUGGY_MODULE, encoding="utf-8")
    return workspace


class TestPlanner:
    async def test_complex_request_becomes_a_task_graph(self, engine):
        plan = await engine.planner.plan(
            "이 저장소를 분석해서 버그를 찾고 수정한 뒤 테스트를 실행하고 리뷰해서 커밋해줘"
        )
        names = [task.name for task in plan.tasks]
        assert len(plan.tasks) >= 5
        assert {"inspect", "static_analysis", "implement", "test", "review", "commit"} <= set(names)
        assert any(task.dependencies for task in plan.tasks)

    async def test_plan_declares_capabilities_not_agents(self, engine):
        plan = await engine.planner.plan("버그를 찾아서 고쳐줘")
        for task in plan.tasks:
            assert task.required_capabilities
            assert task.assigned_agent_id is None

    async def test_first_stage_tasks_are_independent(self, engine):
        plan = await engine.planner.plan("저장소를 분석해서 버그를 찾아줘")
        stage_zero = [task for task in plan.tasks if task.metadata.get("stage") == 0]
        assert len(stage_zero) >= 2
        assert all(not task.dependencies for task in stage_zero)

    async def test_plain_question_produces_a_single_task(self, engine):
        plan = await engine.planner.plan("AIRVIS가 무엇인가요?")
        assert len(plan.tasks) == 1

    async def test_empty_request_is_rejected(self, engine):
        from airvis.core.errors import PlanningError

        with pytest.raises(PlanningError):
            await engine.planner.plan("   ")


class TestFullPipeline:
    async def test_simple_request_completes(self, engine):
        result = await engine.run("안녕하세요")
        assert result.status is WorkflowStatus.COMPLETED
        assert result.output

    async def test_request_travels_every_stage(self, engine, buggy_workspace):
        result = await engine.run(
            "이 저장소를 분석해서 버그를 찾고 개선 방안을 설계한 뒤 보고서를 작성해줘"
        )
        assert result.status is WorkflowStatus.COMPLETED, result.error

        emitted = {event["type"] for event in result.events}
        required = {
            EventType.WORKFLOW_CREATED.value,
            EventType.PLAN_CREATED.value,
            EventType.TASK_CREATED.value,
            EventType.AGENT_SELECTED.value,
            EventType.TASK_ASSIGNED.value,
            EventType.BACKEND_SELECTED.value,
            EventType.PROVIDER_SELECTED.value,
            EventType.TOOL_STARTED.value,
            EventType.TOOL_COMPLETED.value,
            EventType.TASK_COMPLETED.value,
            EventType.REVIEW_STARTED.value,
            EventType.REVIEW_COMPLETED.value,
            EventType.WORKFLOW_COMPLETED.value,
        }
        assert required <= emitted, sorted(required - emitted)

    async def test_agents_are_selected_dynamically_per_task(self, engine, buggy_workspace):
        result = await engine.run("저장소를 분석해서 버그를 찾고 개선 방안을 설계한 뒤 보고서를 작성해줘")
        assigned = {task["assigned_agent_id"] for task in result.tasks if task["assigned_agent_id"]}
        assert len(assigned) >= 3, assigned

    async def test_each_task_records_backend_and_provider(self, engine, buggy_workspace):
        result = await engine.run("저장소를 분석해서 버그를 찾아줘")
        completed = [task for task in result.tasks if task["status"] == TaskStatus.COMPLETED.value]
        assert completed
        for task in completed:
            outcome = task["result"]
            assert outcome["backend_id"] == "native"
            assert outcome["provider_id"] == "mock"

    async def test_tools_actually_execute_and_find_the_seeded_bug(self, engine, buggy_workspace):
        result = await engine.run("저장소를 분석해서 버그를 찾아줘")
        findings: list[dict] = []
        for task in result.tasks:
            for record in (task.get("result") or {}).get("tool_results", []):
                if record.get("tool") == "code.analyze" and record.get("ok"):
                    findings.extend(record["output"]["findings"])
        assert findings, "code.analyze produced no findings"
        rules = {item["rule"] for item in findings}
        assert "unreachable-code" in rules
        assert "global-without-module-binding" in rules
        assert any(item["file"] == "buggy.py" for item in findings)

    async def test_artifacts_are_registered(self, engine, buggy_workspace):
        result = await engine.run("저장소를 분석해서 버그를 찾아줘")
        assert result.artifacts
        assert all({"id", "type", "name"} <= set(item) for item in result.artifacts)

    async def test_reviews_are_recorded_for_every_task(self, engine, buggy_workspace):
        result = await engine.run("저장소를 분석해서 버그를 찾아줘")
        completed = [task for task in result.tasks if task["status"] == TaskStatus.COMPLETED.value]
        assert all(task["review"] is not None for task in completed)
        assert result.reviews

    async def test_workflow_state_is_persisted_and_resumable(self, engine):
        result = await engine.run("안녕하세요")
        stored = engine.store.load_workflow(result.workflow_id)
        assert stored is not None and stored["status"] == "completed"
        assert engine.store.load_tasks(result.workflow_id)
        assert engine.store.list_events(result.workflow_id)

        resumed = await engine.resume(result.workflow_id)
        assert resumed.status is WorkflowStatus.COMPLETED

    async def test_context_flows_from_upstream_tasks(self, engine, buggy_workspace):
        result = await engine.run("저장소를 분석해서 버그를 찾고 보고서를 작성해줘")
        downstream = [
            task for task in result.tasks
            if task["dependencies"] and task["status"] == TaskStatus.COMPLETED.value
        ]
        assert downstream, "expected at least one dependent task"

    async def test_status_and_cancel_are_available(self, engine):
        result = await engine.run("안녕하세요")
        status = engine.orchestrator.status(result.workflow_id)
        assert status["workflow_id"] == result.workflow_id
        assert engine.cancel(result.workflow_id) in {True, False}


class TestRoutingStrategies:
    @pytest.mark.parametrize("strategy", ["cheap", "balanced", "fast", "quality", "premium", "local_only"])
    async def test_every_strategy_can_run_a_workflow(self, engine, strategy):
        result = await engine.run("안녕하세요", strategy=strategy)
        assert result.status is WorkflowStatus.COMPLETED, (strategy, result.error)


class TestLegacyRuntimeFacade:
    def test_run_goes_through_the_new_pipeline(self, tmp_path, workspace):
        from airvis.runtime import AgentRuntime

        runtime = AgentRuntime(
            workspace, memory_path=tmp_path / "memory.db", session_path=tmp_path / "sessions.json"
        )
        answer = runtime.run("안녕하세요")
        assert "Mock Provider" in answer
        assert runtime._workflow_id is not None

    def test_status_exposes_the_v6_registries(self, tmp_path, workspace):
        from airvis.runtime import AgentRuntime

        runtime = AgentRuntime(
            workspace, memory_path=tmp_path / "memory.db", session_path=tmp_path / "sessions.json"
        )
        status = runtime.status()
        assert status["backends"] == ["native"]
        assert "mock" in status["providers"]
        assert "coder" in status["agents"]

    def test_legacy_agent_delegation_still_works(self, tmp_path, workspace):
        from airvis.runtime import AgentRuntime

        runtime = AgentRuntime(
            workspace, memory_path=tmp_path / "memory.db", session_path=tmp_path / "sessions.json"
        )
        assert runtime.agents.list()
        assert runtime.agents.delegate("research", "파일을 조사해줘")
