"""Provider, backend and agent registry plus AgentRouter unit tests."""

from __future__ import annotations

import pytest

from airvis.agents.registry import AgentRegistry
from airvis.agents.router import AgentRouter
from airvis.agents.spec import AgentSpec
from airvis.backends.base import Backend, BackendType, ExecutionRequest, ExecutionResult
from airvis.backends.cli import HermesBackend, OpenClawBackend
from airvis.core.config import RoutingConfig, RoutingStrategy
from airvis.core.errors import (
    BackendUnavailableError,
    InvalidReferenceError,
    NoAgentAvailableError,
    ProviderUnavailableError,
    UnknownAgentError,
    UnknownBackendError,
    UnknownProviderError,
)
from airvis.core.health import HealthState, HealthStatus
from airvis.orchestration.task import Task
from airvis.providers.base import (
    GenerationRequest,
    GenerationResult,
    Message,
    Provider,
    ProviderCapabilities,
)
from airvis.providers.mock import MockProvider
from airvis.providers.registry import ProviderRegistry


class BrokenProvider(Provider):
    id = "broken"
    capabilities = ProviderCapabilities(chat=True)
    default_model = "broken-1"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        raise ProviderUnavailableError("offline", provider=self.id)


class PremiumProvider(Provider):
    id = "premium"
    capabilities = ProviderCapabilities(chat=True, tool_calling=True, reasoning=True)
    default_model = "premium-1"
    models = ("premium-1", "premium-2")
    quality = 0.95
    cost_per_million_input = 10.0
    cost_per_million_output = 30.0

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult(text="premium answer", provider=self.id, model=self.default_model)


class RecordingBackend(Backend):
    id = "recording"
    type = BackendType.CUSTOM
    capabilities = frozenset({"chat"})

    def __init__(self) -> None:
        self.calls: list[ExecutionRequest] = []
        super().__init__()

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        return ExecutionResult(ok=True, output="recorded", backend_id=self.id)

    async def health_check(self) -> HealthStatus:
        import time

        return HealthStatus(HealthState.HEALTHY, "always up", time.time())


class TestProviderRegistry:
    def test_unknown_provider_raises(self, providers: ProviderRegistry):
        with pytest.raises(UnknownProviderError):
            providers.get("nope")

    def test_capability_filtering(self, providers: ProviderRegistry):
        providers.register(PremiumProvider())
        assert [item.id for item in providers.candidates(capability="reasoning")] == ["premium"]

    def test_local_only_filtering(self, providers: ProviderRegistry):
        providers.register(PremiumProvider())
        assert [item.id for item in providers.candidates(local_only=True)] == ["mock"]

    async def test_failover_reaches_the_next_provider(self, providers: ProviderRegistry):
        providers.register(BrokenProvider())
        providers.fallbacks = ["mock"]
        result = await providers.generate(
            GenerationRequest(messages=[Message("user", "hi")]), provider_id="broken"
        )
        assert result.provider == "mock"

    async def test_all_providers_failing_raises(self, health, event_bus):
        registry = ProviderRegistry([BrokenProvider()], health=health, event_bus=event_bus)
        with pytest.raises(ProviderUnavailableError):
            await registry.generate(GenerationRequest(messages=[Message("user", "hi")]))

    async def test_failures_are_recorded_against_health(self, providers: ProviderRegistry):
        providers.register(BrokenProvider())
        providers.fallbacks = ["mock"]
        await providers.generate(GenerationRequest(messages=[Message("user", "hi")]), provider_id="broken")
        assert providers.health.stats("broken").failures == 1

    async def test_health_check_marks_unhealthy_providers(self, providers: ProviderRegistry):
        class Sick(Provider):
            id = "sick"

            async def generate(self, request):  # pragma: no cover - never called
                raise AssertionError

            async def health_check(self):
                raise RuntimeError("probe exploded")

        providers.register(Sick())
        report = await providers.health_check_all()
        assert report["sick"]["state"] == "unhealthy"

    def test_capability_detection_is_explicit(self):
        assert MockProvider().capabilities.supports("streaming")
        assert not MockProvider().capabilities.supports("embeddings")


class TestBackendRegistry:
    def test_native_backend_is_always_present(self, backends):
        assert backends.has("native")

    def test_unknown_backend_raises(self, backends):
        with pytest.raises(UnknownBackendError):
            backends.resolve("nope")

    def test_unhealthy_backend_is_refused(self, backends):
        backends.health.set_health("native", HealthStatus(HealthState.UNHEALTHY, "down"))
        with pytest.raises(BackendUnavailableError):
            backends.resolve("native")

    def test_excluded_backend_is_refused(self, backends):
        with pytest.raises(BackendUnavailableError):
            backends.resolve("native", exclude={"native"})

    async def test_execute_records_success(self, backends, agents):
        backends.register(RecordingBackend())
        agent = agents.get("researcher")
        result = await backends.execute(
            "recording", ExecutionRequest(agent=agent, instruction="do it")
        )
        assert result.ok and backends.health.stats("recording").successes == 1

    async def test_cli_backend_reports_unhealthy_when_binary_missing(self, workspace):
        backend = OpenClawBackend("definitely-not-installed-openclaw", workspace=workspace)
        status = await backend.health_check()
        assert status.state is HealthState.UNHEALTHY

    async def test_cli_backend_raises_instead_of_faking_success(self, workspace, agents):
        backend = HermesBackend("definitely-not-installed-hermes", workspace=workspace)
        with pytest.raises(BackendUnavailableError):
            await backend.execute(ExecutionRequest(agent=agents.get("researcher"), instruction="hi"))


class TestAgentRegistry:
    def test_default_roster_is_registered(self, agents: AgentRegistry):
        assert {"researcher", "coder", "tester", "reviewer"} <= set(agents.names())

    def test_unknown_agent_raises(self, agents: AgentRegistry):
        with pytest.raises(UnknownAgentError):
            agents.get("ghost")

    def test_invalid_backend_reference_is_rejected(self, agents: AgentRegistry):
        with pytest.raises(InvalidReferenceError):
            agents.register(AgentSpec(id="bad", backend_id="nowhere"))

    def test_invalid_provider_reference_is_rejected(self, agents: AgentRegistry):
        with pytest.raises(InvalidReferenceError):
            agents.register(AgentSpec(id="bad", backend_id="native", provider_id="nowhere"))

    def test_invalid_tool_reference_is_rejected(self, agents: AgentRegistry):
        with pytest.raises(InvalidReferenceError):
            agents.register(AgentSpec(id="bad", backend_id="native", tools={"tool.that.does.not.exist"}))

    def test_unsupported_model_is_rejected(self, agents: AgentRegistry, providers: ProviderRegistry):
        providers.register(PremiumProvider())
        with pytest.raises(InvalidReferenceError):
            agents.register(
                AgentSpec(id="bad", backend_id="native", provider_id="premium", model="does-not-exist")
            )

    def test_reference_problems_reports_without_raising(self, agents: AgentRegistry):
        problems = agents.reference_problems(AgentSpec(id="bad", backend_id="nowhere"))
        assert problems and "nowhere" in problems[0]

    def test_capability_lookup(self, agents: AgentRegistry):
        ids = {agent.id for agent in agents.with_capabilities({"test"})}
        assert "tester" in ids

    def test_agents_declare_explicit_backend_references(self, agents: AgentRegistry):
        for agent in agents.all():
            assert agent.backend_id, agent.id
            # the backend must be a registered id, never derived from the agent id
            assert agent.backend_id != agent.id


class TestAgentRouter:
    def test_selects_by_capability(self, router: AgentRouter):
        task = Task(description="run the tests", required_capabilities=["test"])
        assert router.select(task).agent.id == "tester"

    def test_selects_a_reviewer_for_review_work(self, router: AgentRouter):
        task = Task(description="review the diff", required_capabilities=["review"])
        assert router.select(task).agent.id == "reviewer"

    def test_no_capable_agent_raises(self, router: AgentRouter):
        task = Task(description="impossible", required_capabilities=["telepathy"])
        with pytest.raises(NoAgentAvailableError):
            router.select(task)

    def test_required_tool_access_is_enforced(self, router: AgentRouter):
        task = Task(description="push it", required_capabilities=["test"], required_tools=["git.push"])
        with pytest.raises(NoAgentAvailableError):
            router.select(task)

    def test_excluded_agent_is_not_selected(self, router: AgentRouter):
        task = Task(description="run the tests", required_capabilities=["test"])
        task.excluded_agent_ids = ["tester"]
        assert router.select(task).agent.id != "tester"

    def test_pinned_agent_wins(self, router: AgentRouter):
        task = Task(description="anything", required_capabilities=["test"])
        task.forced_agent_id = "researcher"
        assert router.select(task).agent.id == "researcher"

    def test_pinned_unknown_agent_raises(self, router: AgentRouter):
        task = Task(description="anything")
        task.forced_agent_id = "ghost"
        with pytest.raises(UnknownAgentError):
            router.select(task)

    def test_unhealthy_agent_is_skipped(self, router: AgentRouter, health):
        health.set_health("tester", HealthStatus(HealthState.UNHEALTHY, "flaky"))
        task = Task(description="run the tests", required_capabilities=["test"])
        assert router.select(task).agent.id != "tester"

    def test_saturated_agent_is_skipped(self, router: AgentRouter, agents, health):
        agents.get("tester").max_concurrency = 1
        health.acquire("tester")
        task = Task(description="run the tests", required_capabilities=["test"])
        assert router.select(task).agent.id != "tester"

    def test_local_only_strategy_rejects_remote_providers(self, agents, providers, backends, tools, health):
        providers.register(PremiumProvider())
        for agent in agents.all():
            agent.provider_id = "premium"
            agent.model = "premium-1"
        router = AgentRouter(
            agents,
            config=RoutingConfig(strategy=RoutingStrategy.LOCAL_ONLY.value),
            providers=providers,
            backends=backends,
            tools=tools,
            health=health,
        )
        with pytest.raises(NoAgentAvailableError):
            router.select(Task(description="anything", required_capabilities=["test"]))

    def test_cheap_strategy_prefers_the_cheaper_provider(self, agents, providers, backends, tools, health):
        providers.register(PremiumProvider())
        agents.get("tester").provider_id = "premium"
        agents.get("tester").model = "premium-1"
        agents.get("generalist").provider_id = "mock"
        agents.get("generalist").model = None
        cheap = AgentRouter(
            agents, config=RoutingConfig(strategy="cheap"), providers=providers,
            backends=backends, tools=tools, health=health,
        )
        task = Task(description="run the tests", required_capabilities=["test"])
        assert cheap.select(task).agent.id == "generalist"

    def test_quality_strategy_prefers_the_stronger_provider(self, agents, providers, backends, tools, health):
        providers.register(PremiumProvider())
        agents.get("tester").provider_id = "premium"
        agents.get("tester").model = "premium-1"
        agents.get("generalist").provider_id = "mock"
        quality = AgentRouter(
            agents, config=RoutingConfig(strategy="quality"), providers=providers,
            backends=backends, tools=tools, health=health,
        )
        task = Task(description="run the tests", required_capabilities=["test"])
        assert quality.select(task).agent.id == "tester"

    def test_weights_are_configurable(self):
        weights = RoutingConfig(strategy="balanced", weights={"cost": 99.0}).resolved_weights()
        assert weights["cost"] == 99.0

    def test_rank_exposes_score_components(self, router: AgentRouter):
        ranking = router.rank(Task(description="run the tests", required_capabilities=["test"]))
        top = ranking[0]
        assert top.rejected is None
        assert {"capability", "reliability", "cost", "latency", "workload"} <= set(top.components)
