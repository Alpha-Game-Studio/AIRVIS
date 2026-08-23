"""Failure-path tests: every documented failure mode must be handled, not crash."""

from __future__ import annotations

import asyncio
import time

import pytest

from airvis.agents.spec import AgentSpec
from airvis.backends.base import Backend, BackendType, ExecutionRequest, ExecutionResult
from airvis.core.errors import (
    BackendUnavailableError,
    DAGCycleError,
    ProviderUnavailableError,
    RateLimitError,
    ToolExecutionError,
)
from airvis.core.health import HealthState, HealthStatus
from airvis.orchestration.repair import FailureCategory, RepairStrategy
from airvis.orchestration.task import Plan, Task, ToolStep, WorkflowStatus
from airvis.providers.base import GenerationRequest, GenerationResult, Provider, ProviderCapabilities


class AlwaysFailingBackend(Backend):
    id = "failing"
    type = BackendType.CUSTOM
    capabilities = frozenset({"chat"})

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or BackendUnavailableError("backend is down", backend="failing")
        self.calls = 0
        super().__init__()

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls += 1
        raise self.error

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "reachable but always errors", time.time())


class SlowBackend(Backend):
    id = "slow"
    type = BackendType.CUSTOM
    capabilities = frozenset({"chat"})

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        await asyncio.sleep(30)
        return ExecutionResult(ok=True, output="too late", backend_id=self.id)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "up", time.time())


class RateLimitedProvider(Provider):
    id = "throttled"
    capabilities = ProviderCapabilities(chat=True)
    default_model = "throttled-1"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        raise RateLimitError("slow down", provider=self.id)


def register_agent(engine, agent_id: str, capability: str, **kwargs) -> AgentSpec:
    spec = AgentSpec(
        id=agent_id,
        role=agent_id,
        capabilities=frozenset({capability}),
        tools=frozenset(kwargs.pop("tools", set())),
        permissions=frozenset(kwargs.pop("permissions", set())),
        backend_id=kwargs.pop("backend_id", "native"),
        provider_id=kwargs.pop("provider_id", "mock"),
        model=kwargs.pop("model", None),
        **kwargs,
    )
    return engine.agents.register(spec)


def single_task_plan(request: str, **task_kwargs) -> Plan:
    task = Task(description=request, **task_kwargs)
    return Plan(request=request, tasks=[task])


class TestProviderFailures:
    async def test_provider_unavailable_falls_back(self, engine):
        class Broken(Provider):
            id = "broken"
            capabilities = ProviderCapabilities(chat=True)

            async def generate(self, request):
                raise ProviderUnavailableError("offline", provider=self.id)

        engine.providers.register(Broken())
        engine.providers.fallbacks = ["mock"]
        register_agent(engine, "broken-agent", "brokencap", provider_id="broken")
        plan = single_task_plan("작업", name="broken", required_capabilities=["brokencap"])
        result = await engine.run("작업", plan=plan)
        assert result.status is WorkflowStatus.COMPLETED
        assert result.tasks[0]["result"]["provider_id"] == "mock"

    async def test_every_provider_failing_is_reported_not_crashed(self, engine):
        class Broken(Provider):
            id = "allbroken"
            capabilities = ProviderCapabilities(chat=True)

            async def generate(self, request):
                raise ProviderUnavailableError("offline", provider=self.id)

        engine.providers.unregister("mock")
        engine.providers.register(Broken())
        engine.providers.fallbacks = []
        register_agent(engine, "all-broken-agent", "allbrokencap", provider_id="allbroken")
        plan = single_task_plan("작업", name="allbroken", required_capabilities=["allbrokencap"])
        result = await engine.run("작업", plan=plan)
        assert result.status is WorkflowStatus.FAILED
        assert result.repairs

    async def test_rate_limit_is_classified_and_repaired(self, engine):
        engine.providers.register(RateLimitedProvider())
        engine.providers.fallbacks = ["mock"]
        register_agent(engine, "throttled-agent", "throttledcap", provider_id="throttled")
        plan = single_task_plan("작업", name="throttled", required_capabilities=["throttledcap"])
        result = await engine.run("작업", plan=plan)
        # The provider chain already recovers, so the task completes on mock.
        assert result.status is WorkflowStatus.COMPLETED
        assert engine.providers.health.stats("throttled").failures >= 1

    async def test_rate_limit_marks_the_provider_unusable(self, providers):
        providers.register(RateLimitedProvider())
        providers.fallbacks = ["mock"]
        from airvis.providers.base import Message

        await providers.generate(
            GenerationRequest(messages=[Message("user", "hi")]), provider_id="throttled"
        )
        assert not providers.health.is_usable("throttled")


class TestBackendFailures:
    async def test_backend_error_repairs_by_switching_backend(self, engine):
        failing = AlwaysFailingBackend()
        engine.backends.register(failing)
        register_agent(engine, "failing-agent", "failcap", backend_id="failing")
        plan = single_task_plan("작업", name="failing", required_capabilities=["failcap"])
        result = await engine.run("작업", plan=plan)

        assert failing.calls >= 1
        assert result.repairs[0]["analysis"]["category"] == FailureCategory.BACKEND_ERROR.value
        assert result.repairs[0]["strategy"] == RepairStrategy.CHANGE_BACKEND.value
        # The same agent is re-run on a healthy backend rather than being dropped.
        assert result.status is WorkflowStatus.COMPLETED, result.error
        assert result.tasks[0]["result"]["backend_id"] == "native"
        assert result.tasks[0]["result"]["agent_id"] == "failing-agent"

    async def test_repair_terminates_when_no_backend_can_serve(self, engine):
        engine.backends.register(AlwaysFailingBackend())
        second = AlwaysFailingBackend()
        second.id = "failing2"
        engine.backends.register(second)
        register_agent(engine, "doomed-agent", "doomedcap", backend_id="failing")
        plan = single_task_plan("작업", name="doomed", required_capabilities=["doomedcap"])
        plan.tasks[0].excluded_backend_ids = ["native"]
        result = await engine.run("작업", plan=plan)
        assert result.status is WorkflowStatus.FAILED
        strategies = [item["strategy"] for item in result.repairs]
        # The loop must terminate in a give-up strategy, never spin forever.
        assert strategies[-1] in {RepairStrategy.ABORT.value, RepairStrategy.HUMAN_REVIEW.value}
        assert len(strategies) == len(set(strategies)), "a strategy was retried"

    async def test_repair_switches_to_a_healthy_backend(self, engine):
        engine.backends.register(AlwaysFailingBackend())
        register_agent(engine, "switchable", "switchcap", backend_id="failing")
        plan = single_task_plan("작업", name="switchable", required_capabilities=["switchcap"])
        result = await engine.run("작업", plan=plan)
        strategies = [item["strategy"] for item in result.repairs]
        assert RepairStrategy.CHANGE_BACKEND.value in strategies

    async def test_unhealthy_backend_makes_the_agent_unroutable(self, engine):
        engine.backends.register(AlwaysFailingBackend())
        engine.health.set_health("failing", HealthStatus(HealthState.UNHEALTHY, "down"))
        register_agent(engine, "unhealthy-agent", "unhealthycap", backend_id="failing")
        plan = single_task_plan("작업", name="unhealthy", required_capabilities=["unhealthycap"])
        result = await engine.run("작업", plan=plan)
        assert result.status is WorkflowStatus.FAILED

    async def test_timeout_is_bounded(self, engine):
        engine.backends.register(SlowBackend())
        register_agent(engine, "slow-agent", "slowcap", backend_id="slow", timeout=0.2)
        task = Task(description="작업", name="slow", required_capabilities=["slowcap"], timeout=0.2)
        engine.config.workflow.task_timeout = 0.2
        engine.config.repair.max_repairs_per_task = 1
        result = await asyncio.wait_for(
            engine.run("작업", plan=Plan(request="작업", tasks=[task])), timeout=120
        )
        assert result.status is WorkflowStatus.FAILED


class TestToolFailures:
    async def test_tool_error_is_captured_in_the_task_result(self, engine):
        from airvis.tools.base import FunctionTool, RiskLevel

        def explode() -> str:
            raise ValueError("tool exploded")

        engine.tools.register(FunctionTool("custom.explode", "always fails", RiskLevel.SAFE, explode))
        register_agent(engine, "exploder", "explodecap", tools={"custom.explode"})
        engine.config.repair.max_repairs_per_task = 0  # inspect the first attempt verbatim
        task = Task(
            description="작업",
            name="exploder",
            required_capabilities=["explodecap"],
            tool_plan=[ToolStep("custom.explode", {}, optional=True)],
        )
        result = await engine.run("작업", plan=Plan(request="작업", tasks=[task]))
        records = result.tasks[0]["result"]["tool_results"]
        assert records and records[0]["ok"] is False
        assert "tool exploded" in records[0]["error"]

    async def test_mandatory_tool_failure_fails_the_task(self, engine):
        from airvis.tools.base import FunctionTool, RiskLevel

        def explode() -> str:
            raise ToolExecutionError("hard failure", tool="custom.hard")

        engine.tools.register(FunctionTool("custom.hard", "always fails", RiskLevel.SAFE, explode))
        register_agent(engine, "hard-failer", "hardcap", tools={"custom.hard"})
        task = Task(
            description="작업",
            name="hard",
            required_capabilities=["hardcap"],
            tool_plan=[ToolStep("custom.hard", {}, optional=False)],
        )
        result = await engine.run("작업", plan=Plan(request="작업", tasks=[task]))
        assert result.status is WorkflowStatus.FAILED
        assert result.repairs

    async def test_permission_denied_is_classified_as_such(self, engine):
        register_agent(engine, "unprivileged", "unprivcap", tools={"filesystem.write"})
        task = Task(
            description="작업",
            name="unprivileged",
            required_capabilities=["unprivcap"],
            tool_plan=[ToolStep("filesystem.write", {"path": "x.txt", "content": "y"}, optional=False)],
        )
        result = await engine.run("작업", plan=Plan(request="작업", tasks=[task]))
        categories = {item["analysis"]["category"] for item in result.repairs}
        assert FailureCategory.PERMISSION_ERROR.value in categories

    async def test_denied_tool_cannot_be_bypassed(self, engine):
        from airvis.core.errors import PermissionDeniedError

        engine.permissions.config.denied_tools = ["filesystem.read"]
        with pytest.raises(PermissionDeniedError):
            await engine.tools.call("filesystem.read", {"path": "notes.txt"}, confirm=True)


class TestRoutingFailures:
    async def test_unroutable_task_fails_cleanly(self, engine):
        plan = single_task_plan("작업", name="impossible", required_capabilities=["telekinesis"])
        result = await engine.run("작업", plan=plan)
        assert result.status is WorkflowStatus.FAILED
        categories = {item["analysis"]["category"] for item in result.repairs}
        assert FailureCategory.ROUTING_ERROR.value in categories

    async def test_pinned_unknown_agent_fails_cleanly(self, engine):
        task = Task(description="작업", name="ghost")
        task.forced_agent_id = "does-not-exist"
        result = await engine.run("작업", plan=Plan(request="작업", tasks=[task]))
        assert result.status is WorkflowStatus.FAILED


class TestGraphFailures:
    async def test_cycle_is_rejected_before_execution(self, engine):
        first = Task(description="a", name="a")
        second = Task(description="b", name="b")
        first.dependencies = [second.id]
        second.dependencies = [first.id]
        result = await engine.run("작업", plan=Plan(request="작업", tasks=[first, second]))
        assert result.status is WorkflowStatus.FAILED
        assert "cycle" in (result.error or "").lower()

    async def test_unknown_dependency_is_rejected(self, engine):
        task = Task(description="a", name="a", dependencies=["nope"])
        result = await engine.run("작업", plan=Plan(request="작업", tasks=[task]))
        assert result.status is WorkflowStatus.FAILED

    def test_cycle_detection_raises_the_specific_error(self):
        from airvis.orchestration.dag import validate_graph

        first, second = Task(description="a"), Task(description="b")
        first.dependencies, second.dependencies = [second.id], [first.id]
        with pytest.raises(DAGCycleError):
            validate_graph([first, second])


class TestReviewFailures:
    async def test_review_rejection_drives_the_repair_loop(self, engine):
        # A code task that changes nothing must be rejected by the quality gate.
        register_agent(engine, "lazy-coder", "code", tools={"filesystem.read"})
        task = Task(description="코드를 수정한다", name="lazy", required_capabilities=["code"])
        task.forced_agent_id = "lazy-coder"
        result = await engine.run("작업", plan=Plan(request="작업", tasks=[task]))
        assert result.status is WorkflowStatus.FAILED
        assert any(item["status"] == "FAIL" for item in result.reviews)
        assert result.repairs

    async def test_repair_budget_stops_the_loop(self, engine):
        engine.config.repair.max_repairs_per_task = 2
        engine.backends.register(AlwaysFailingBackend())
        second = AlwaysFailingBackend()
        second.id = "failing2"
        engine.backends.register(second)
        register_agent(engine, "budgeted", "budgetcap", backend_id="failing")
        plan = single_task_plan("작업", name="budgeted", required_capabilities=["budgetcap"])
        plan.tasks[0].excluded_backend_ids = ["native"]
        result = await engine.run("작업", plan=plan)
        assert len(result.repairs) <= 3  # bounded attempts plus the terminal decision
        assert result.repairs[-1]["strategy"] in {
            RepairStrategy.ABORT.value,
            RepairStrategy.HUMAN_REVIEW.value,
        }

    async def test_finalizer_still_runs_after_an_upstream_failure(self, engine):
        engine.backends.register(AlwaysFailingBackend())
        register_agent(engine, "doomed", "doomcap", backend_id="failing")
        failing = Task(description="실패하는 작업", name="doomed", required_capabilities=["doomcap"])
        failing.excluded_backend_ids = ["native"]  # no healthy alternative to switch to
        report = Task(
            description="보고서를 작성한다",
            name="report",
            required_capabilities=["report"],
            dependencies=[failing.id],
            finalizer=True,
        )
        result = await engine.run("작업", plan=Plan(request="작업", tasks=[failing, report]))
        statuses = {task["name"]: task["status"] for task in result.tasks}
        assert statuses["doomed"] == "failed"
        assert statuses["report"] == "completed"
