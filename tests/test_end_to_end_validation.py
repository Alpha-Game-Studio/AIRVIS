"""The AIRVIS V6 acceptance scenario.

    "Analyze this repository, find a bug, propose a fix, implement the fix,
     run tests, review the result, and produce a final report."

The request must travel Orchestrator -> Planner -> DAG -> AgentRouter -> Agent
-> Backend -> Provider -> Tool -> Artifact -> Review -> Repair -> Final result,
and the effects must be real: the file on disk changes and the test suite that
failed before the run passes after it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from airvis.core.events import EventType
from airvis.orchestration.task import TaskStatus, WorkflowStatus
from airvis.providers.base import (
    GenerationRequest,
    GenerationResult,
    Provider,
    ProviderCapabilities,
    ToolCall,
    Usage,
)

BUGGY_SOURCE = '''"""Registry helper with a real defect: _registry is never bound at module level."""


def get_registry():
    global _registry
    if _registry is None:
        _registry = {}
    return _registry
'''

FIXED_SOURCE = '''"""Registry helper. _registry is now bound at module level."""

_registry = None


def get_registry():
    global _registry
    if _registry is None:
        _registry = {}
    return _registry
'''

FAILING_TEST = '''from buggy_app import get_registry


def test_registry_starts_empty():
    assert get_registry() == {}
'''

REQUEST = "이 저장소를 분석해서 버그를 찾고 수정한 뒤 테스트를 실행하고 리뷰해서 보고서를 작성해줘"


class CodingProvider(Provider):
    """A provider that emits real tool calls, standing in for a coding model.

    Its first answer for the coder agent is deliberately unhelpful so the review
    gate rejects it and the repair system has to drive a second attempt.
    """

    id = "coding"
    capabilities = ProviderCapabilities(chat=True, tool_calling=True, structured_output=True)
    default_model = "coding-1"
    models = ("coding-1", "coding-2")
    quality = 0.9

    def __init__(self, target: str, content: str) -> None:
        self.target = target
        self.content = content
        self.coder_calls = 0
        self.calls: list[str] = []
        super().__init__()

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        agent_id = str(request.metadata.get("agent_id", ""))
        self.calls.append(agent_id)
        result = GenerationResult(provider=self.id, model=self.resolve_model(request.model), usage=Usage(10, 5))

        if agent_id == "coder":
            self.coder_calls += 1
            if self.coder_calls == 1:
                result.text = "수정이 필요해 보입니다. 나중에 처리하겠습니다."
                return result
            if self.coder_calls == 2:
                result.text = "결함을 수정합니다."
                result.tool_calls = [
                    ToolCall(name="filesystem.write", arguments={"path": self.target, "content": self.content})
                ]
                return result

        observations = [item for item in request.messages if item.role == "tool"]
        result.text = f"[{agent_id or 'agent'}] 관찰 {len(observations)}건을 근거로 작업을 마쳤습니다."
        return result


def _suite_passes(root: Path) -> bool:
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", str(root)],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return completed.returncode == 0


@dataclass
class Scenario:
    engine: Any
    repo: Path
    provider: CodingProvider
    result: Any


@pytest.fixture(scope="class")
def scenario(tmp_path_factory) -> Scenario:
    """Build the repository, run the scenario once, share it across assertions."""
    from airvis.core.asyncutil import run_blocking
    from airvis.core.config import AirvisConfig
    from airvis.engine import AirvisEngine
    from airvis.security.permissions import always_approve

    root = tmp_path_factory.mktemp("repo")
    state = tmp_path_factory.mktemp("state")
    (root / "buggy_app.py").write_text(BUGGY_SOURCE, encoding="utf-8")
    (root / "test_buggy_app.py").write_text(FAILING_TEST, encoding="utf-8")
    assert not _suite_passes(root), "the seeded test suite must fail before the run"

    config = AirvisConfig()
    config.workspace = str(root)
    config.workflow.max_concurrency = 4
    engine = AirvisEngine(
        config,
        workspace=root,
        approval_handler=always_approve,
        state_path=state / "state.db",
        memory_path=state / "memory.db",
        artifact_root=state / "artifacts",
        environ={},
    )
    provider = CodingProvider("buggy_app.py", FIXED_SOURCE)
    engine.providers.register(provider)
    engine.providers.fallbacks = ["mock"]
    for agent in engine.agents.all():
        agent.provider_id = provider.id
        agent.model = provider.default_model

    return Scenario(engine=engine, repo=root, provider=provider, result=run_blocking(engine.run(REQUEST)))


class TestAcceptanceScenario:
    @pytest.fixture(autouse=True)
    def _bind(self, scenario: Scenario):
        self.engine = scenario.engine
        self.repo = scenario.repo
        self.provider = scenario.provider
        self.result = scenario.result

    # -- 1. planning -----------------------------------------------------------

    def test_the_request_was_decomposed_into_a_dependency_graph(self):
        names = [task["name"] for task in self.result.tasks]
        assert {"inspect", "static_analysis", "diagnose", "implement", "test", "review", "report"} <= set(names)
        assert any(task["dependencies"] for task in self.result.tasks)

    def test_independent_first_stage_tasks_have_no_dependencies(self):
        stage_zero = [task for task in self.result.tasks if task["name"] in {"inspect", "static_analysis"}]
        assert stage_zero and all(not task["dependencies"] for task in stage_zero)

    # -- 2. routing ------------------------------------------------------------

    def test_each_task_was_routed_to_a_distinct_specialised_agent(self):
        assignments = {task["name"]: task["assigned_agent_id"] for task in self.result.tasks}
        assert assignments["implement"] == "coder"
        assert assignments["test"] == "tester"
        assert assignments["review"] == "reviewer"
        assert assignments["report"] == "reporter"
        assert len(set(assignments.values())) >= 5

    def test_agent_backend_and_provider_are_explicit_on_every_result(self):
        for task in self.result.tasks:
            outcome = task["result"]
            if task["status"] != TaskStatus.COMPLETED.value:
                continue
            assert outcome["agent_id"] and outcome["backend_id"] == "native"
            assert outcome["provider_id"] == "coding"
            assert outcome["model"] == "coding-1"

    def test_the_provider_was_actually_invoked_per_agent(self):
        assert {"researcher", "coder", "tester", "reviewer", "reporter"} <= set(self.provider.calls)

    # -- 3. tools and artifacts ------------------------------------------------

    def test_static_analysis_found_the_seeded_defect(self):
        findings = [
            finding
            for task in self.result.tasks
            for record in (task.get("result") or {}).get("tool_results", [])
            if record.get("tool") == "code.analyze" and record.get("ok")
            for finding in record["output"]["findings"]
        ]
        assert any(
            item["rule"] == "global-without-module-binding" and item["file"] == "buggy_app.py"
            for item in findings
        ), findings

    def test_the_fix_was_written_to_disk(self):
        assert (self.repo / "buggy_app.py").read_text(encoding="utf-8") == FIXED_SOURCE

    def test_the_test_suite_was_executed_by_the_pipeline(self):
        runs = [
            record
            for task in self.result.tasks
            for record in (task.get("result") or {}).get("tool_results", [])
            if record.get("tool") == "test.run"
        ]
        assert runs, "the tester agent never executed test.run"
        assert runs[-1]["ok"], runs[-1]

    def test_the_previously_failing_suite_now_passes(self):
        assert _suite_passes(self.repo)

    def test_artifacts_were_registered_for_the_work_products(self):
        kinds = {item["type"] for item in self.result.artifacts}
        assert {"analysis", "file", "test_result"} <= kinds, kinds

    # -- 4. review and repair --------------------------------------------------

    def test_the_review_gate_rejected_the_first_attempt(self):
        assert any(item["status"] == "FAIL" for item in self.result.reviews)

    def test_a_repair_strategy_was_applied(self):
        assert self.result.repairs, "the review rejection should have triggered a repair"
        strategies = [item["strategy"] for item in self.result.repairs]
        assert "MODIFY_CONTEXT" in strategies or "RETRY" in strategies
        assert len(strategies) == len(set(strategies)), "a repair strategy was retried"

    def test_the_repaired_task_finally_passed_review(self):
        implement = next(task for task in self.result.tasks if task["name"] == "implement")
        assert implement["status"] == TaskStatus.COMPLETED.value
        assert implement["review"]["status"] == "PASS"
        assert implement["repair_attempts"] >= 1

    # -- 5. result and observability -------------------------------------------

    def test_the_workflow_completed(self):
        assert self.result.status is WorkflowStatus.COMPLETED, self.result.error

    def test_the_final_report_is_the_reporter_output(self):
        report = next(task for task in self.result.tasks if task["name"] == "report")
        assert self.result.output == report["result"]["output"]
        assert self.result.output.strip()

    def test_the_full_event_chain_was_emitted(self):
        emitted = {event["type"] for event in self.result.events}
        required = {
            EventType.WORKFLOW_CREATED.value,
            EventType.PLAN_CREATED.value,
            EventType.TASK_CREATED.value,
            EventType.TASK_ASSIGNED.value,
            EventType.AGENT_SELECTED.value,
            EventType.BACKEND_SELECTED.value,
            EventType.PROVIDER_SELECTED.value,
            EventType.TOOL_STARTED.value,
            EventType.TOOL_COMPLETED.value,
            EventType.ARTIFACT_CREATED.value,
            EventType.TASK_STARTED.value,
            EventType.TASK_COMPLETED.value,
            EventType.REVIEW_STARTED.value,
            EventType.REVIEW_COMPLETED.value,
            EventType.REPAIR_STARTED.value,
            EventType.REPAIR_COMPLETED.value,
            EventType.WORKFLOW_COMPLETED.value,
        }
        assert required <= emitted, sorted(required - emitted)

    def test_events_carry_the_routing_identity(self):
        selections = [
            event for event in self.result.events if event["type"] == EventType.PROVIDER_SELECTED.value
        ]
        assert selections
        assert all(event["provider_id"] and event["model"] for event in selections)

    def test_the_run_is_persisted_and_recoverable(self):
        stored = self.engine.store.load_workflow(self.result.workflow_id)
        assert stored is not None and stored["status"] == "completed"
        assert len(self.engine.store.load_tasks(self.result.workflow_id)) == len(self.result.tasks)
        assert self.engine.store.list_events(self.result.workflow_id)
        assert self.engine.store.list_repairs(self.result.workflow_id)
