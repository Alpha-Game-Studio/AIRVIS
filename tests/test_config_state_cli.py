"""Configuration, events, persistence, MCP and CLI tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from airvis.core.config import AirvisConfig, RoutingConfig, RoutingStrategy
from airvis.core.errors import ConfigError
from airvis.core.events import Event, EventBus, EventType
from airvis.state.store import StateStore


class TestConfiguration:
    def test_defaults_are_usable(self):
        config = AirvisConfig()
        assert config.routing.strategy == RoutingStrategy.BALANCED.value
        assert config.workflow.max_concurrency == 8
        assert config.repair.max_retries == 3

    def test_loads_yaml(self, tmp_path: Path):
        yaml = pytest.importorskip("yaml")
        path = tmp_path / "airvis.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "routing": {"strategy": "quality"},
                    "agents": {"default_timeout": 42},
                    "workflow": {"max_concurrency": 3},
                    "security": {"default_high_risk_policy": "deny"},
                }
            ),
            encoding="utf-8",
        )
        config = AirvisConfig.load(path, environ={})
        assert config.routing.strategy == "quality"
        assert config.agents.default_timeout == 42
        assert config.workflow.max_concurrency == 3
        assert config.security.default_high_risk_policy == "deny"

    def test_loads_json(self, tmp_path: Path):
        path = tmp_path / "airvis.json"
        path.write_text(json.dumps({"repair": {"max_retries": 7}}), encoding="utf-8")
        assert AirvisConfig.load(path, environ={}).repair.max_retries == 7

    def test_unknown_key_is_rejected(self, tmp_path: Path):
        path = tmp_path / "airvis.json"
        path.write_text(json.dumps({"routing": {"nope": 1}}), encoding="utf-8")
        with pytest.raises(ConfigError):
            AirvisConfig.load(path, environ={})

    def test_missing_file_is_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError):
            AirvisConfig.load(tmp_path / "absent.yaml", environ={})

    def test_environment_overrides_the_file(self, tmp_path: Path):
        path = tmp_path / "airvis.json"
        path.write_text(json.dumps({"routing": {"strategy": "quality"}}), encoding="utf-8")
        config = AirvisConfig.load(path, environ={"AIRVIS_ROUTING_STRATEGY": "cheap"})
        assert config.routing.strategy == "cheap"

    def test_legacy_privacy_switch_maps_to_local_only(self):
        config = AirvisConfig.load(environ={"AIRVIS_PRIVACY_MODE": "LOCAL ONLY"})
        assert config.routing.strategy == RoutingStrategy.LOCAL_ONLY.value

    def test_env_coercion(self):
        config = AirvisConfig.load(
            environ={
                "AIRVIS_MAX_CONCURRENCY": "2",
                "AIRVIS_REVIEW_ENABLED": "false",
                "AIRVIS_REVIEW_MIN_SCORE": "0.9",
                "AIRVIS_FALLBACK_PROVIDER": "mock,ollama",
            }
        )
        assert config.workflow.max_concurrency == 2
        assert config.review.enabled is False
        assert config.review.min_score == 0.9
        assert config.providers.fallbacks == ["mock", "ollama"]

    def test_unknown_strategy_is_rejected(self):
        with pytest.raises(ConfigError):
            RoutingConfig(strategy="telepathic").resolved_weights()

    def test_weights_exist_for_every_strategy(self):
        for strategy in RoutingStrategy:
            weights = RoutingConfig(strategy=strategy.value).resolved_weights()
            assert {"capability", "cost", "latency", "workload", "quality"} <= set(weights)

    def test_round_trips_through_a_dict(self):
        config = AirvisConfig()
        assert AirvisConfig.from_dict(config.to_dict()).to_dict() == config.to_dict()


class TestEventBus:
    def test_handlers_receive_events(self):
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        bus.publish(EventType.TASK_STARTED, task_id="t1")
        assert len(seen) == 1 and seen[0].task_id == "t1"

    def test_handlers_can_filter_by_type(self):
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append, types=[EventType.TASK_FAILED])
        bus.publish(EventType.TASK_STARTED, task_id="t1")
        bus.publish(EventType.TASK_FAILED, task_id="t2")
        assert [event.task_id for event in seen] == ["t2"]

    def test_a_broken_handler_does_not_break_the_pipeline(self):
        bus = EventBus()
        survivors: list[Event] = []

        def explode(_: Event) -> None:
            raise RuntimeError("handler bug")

        bus.subscribe(explode)
        bus.subscribe(survivors.append)
        bus.publish(EventType.TASK_STARTED)
        assert len(survivors) == 1

    def test_unsubscribe(self):
        bus = EventBus()
        seen: list[Event] = []
        cancel = bus.subscribe(seen.append)
        cancel()
        bus.publish(EventType.TASK_STARTED)
        assert not seen

    def test_history_is_scoped_by_workflow(self):
        bus = EventBus()
        bus.publish(EventType.TASK_STARTED, workflow_id="a")
        bus.publish(EventType.TASK_STARTED, workflow_id="b")
        assert len(bus.history(workflow_id="a")) == 1

    def test_events_carry_the_documented_fields(self):
        event = Event(
            type=EventType.TASK_COMPLETED,
            workflow_id="w",
            task_id="t",
            agent_id="a",
            backend_id="b",
            provider_id="p",
            model="m",
            duration_ms=1.5,
            status="ok",
        )
        payload = event.to_dict()
        assert {
            "timestamp", "workflow_id", "task_id", "agent_id", "backend_id",
            "provider_id", "model", "duration_ms", "status", "metadata",
        } <= set(payload)


class TestStateStore:
    def test_round_trips_a_workflow(self, tmp_path: Path):
        store = StateStore(tmp_path / "state.db")
        store.save_workflow({"workflow_id": "w1", "request": "hi", "status": "running"})
        assert store.load_workflow("w1")["status"] == "running"

    def test_updates_preserve_creation_time(self, tmp_path: Path):
        store = StateStore(tmp_path / "state.db")
        store.save_workflow({"workflow_id": "w1", "request": "hi", "status": "running"})
        created = store.list_workflows()[0]["created_at"]
        store.save_workflow({"workflow_id": "w1", "request": "hi", "status": "completed"})
        assert store.list_workflows()[0]["created_at"] == created

    def test_tasks_events_and_artifacts_are_scoped(self, tmp_path: Path):
        store = StateStore(tmp_path / "state.db")
        store.save_task({"id": "t1", "workflow_id": "w1", "status": "queued"})
        store.save_event({"id": "e1", "workflow_id": "w1", "type": "task.created", "timestamp": 1.0})
        store.save_artifact({"id": "a1", "workflow_id": "w1", "task_id": "t1", "type": "report"})
        store.save_review("w1", {"id": "r1", "task_id": "t1", "status": "PASS"})
        store.save_repair("w1", "t1", {"id": "p1", "strategy": "RETRY"})
        assert len(store.load_tasks("w1")) == 1
        assert len(store.list_events("w1")) == 1
        assert len(store.list_artifacts("w1")) == 1
        assert len(store.list_reviews("w1")) == 1
        assert len(store.list_repairs("w1")) == 1

    def test_delete_removes_everything(self, tmp_path: Path):
        store = StateStore(tmp_path / "state.db")
        store.save_workflow({"workflow_id": "w1", "request": "hi", "status": "done"})
        store.save_task({"id": "t1", "workflow_id": "w1", "status": "queued"})
        store.delete_workflow("w1")
        assert store.load_workflow("w1") is None and store.load_tasks("w1") == []

    def test_disabled_store_is_a_no_op(self, tmp_path: Path):
        store = StateStore(tmp_path / "state.db", enabled=False)
        store.save_workflow({"workflow_id": "w1", "request": "hi", "status": "done"})
        assert store.load_workflow("w1") is None


MCP_SERVER = '''
import json, sys

def send(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method, request_id = message.get("method"), message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id,
              "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
            {"name": "echo", "description": "echo the input",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                             "required": ["text"]}}]}})
    elif method == "tools/call":
        text = message["params"]["arguments"].get("text", "")
        send({"jsonrpc": "2.0", "id": request_id,
              "result": {"content": [{"type": "text", "text": f"echo:{text}"}]}})
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
'''


class TestMCPIntegration:
    @pytest.fixture
    def mcp_config(self, tmp_path: Path):
        from airvis.core.config import MCPConfig, MCPServerConfig

        script = tmp_path / "fake_mcp_server.py"
        script.write_text(MCP_SERVER, encoding="utf-8")
        return MCPConfig(
            enabled=True,
            servers=[MCPServerConfig(name="fake", command=sys.executable, args=[str(script)])],
        )

    async def test_tools_are_discovered_and_registered(self, mcp_config, tools):
        from airvis.mcp.integration import close_mcp_tools, register_mcp_tools

        discovered = await register_mcp_tools(mcp_config, tools)
        try:
            assert discovered == ["mcp.fake.echo"]
            assert tools.has("mcp.fake.echo")
            schema = tools.get("mcp.fake.echo").schema()
            assert schema["risk"] == "MEDIUM" and "mcp" in schema["required_permissions"]
        finally:
            await close_mcp_tools(tools)

    async def test_mcp_tools_go_through_the_permission_system(self, mcp_config, tools):
        from airvis.core.errors import PermissionDeniedError
        from airvis.mcp.integration import close_mcp_tools, register_mcp_tools

        await register_mcp_tools(mcp_config, tools)
        try:
            # The tool needs the 'mcp' permission, which no caller holds by default.
            with pytest.raises(PermissionDeniedError):
                await tools.call("mcp.fake.echo", {"text": "hi"})
        finally:
            await close_mcp_tools(tools)

    async def test_mcp_tool_executes_when_permitted(self, mcp_config, tools):
        from airvis.mcp.integration import close_mcp_tools, register_mcp_tools
        from airvis.security.permissions import always_approve

        await register_mcp_tools(mcp_config, tools)
        try:
            result = await tools.call(
                "mcp.fake.echo",
                {"text": "hi"},
                agent_permissions={"mcp", "network"},
                approval_handler=always_approve,
            )
            assert result.output == "echo:hi"
        finally:
            await close_mcp_tools(tools)

    async def test_a_broken_server_never_breaks_startup(self, tools):
        from airvis.core.config import MCPConfig, MCPServerConfig
        from airvis.mcp.integration import register_mcp_tools

        config = MCPConfig(enabled=True, servers=[MCPServerConfig(name="ghost", command="not-a-real-binary")])
        assert await register_mcp_tools(config, tools) == []


class TestDoctor:
    def test_reports_a_healthy_engine(self, engine):
        from airvis.doctor import run_checks, summarize

        report = summarize(run_checks(engine))
        names = {item["name"] for item in report["checks"]}
        assert {"engine", "providers", "backends", "tools", "agents", "agents:references"} <= names
        assert report["ok"], [item for item in report["checks"] if not item["ok"]]

    def test_detects_a_broken_agent_reference(self, engine):
        from airvis.agents.spec import AgentSpec
        from airvis.doctor import run_checks

        engine.agents.register(AgentSpec(id="broken", backend_id="ghost"), validate=False)
        check = next(item for item in run_checks(engine) if item["name"] == "agents:references")
        assert not check["ok"] and "ghost" in check["detail"]


class TestCLI:
    def _run(self, capsys, argv: list[str]) -> tuple[int, str]:
        from airvis.cli import main

        code = main(argv)
        return code, capsys.readouterr().out

    def test_version(self, capsys):
        code, out = self._run(capsys, ["version"])
        assert code == 0 and out.strip().startswith("6.")

    def test_status(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "status"])
        assert code == 0 and json.loads(out)["backends"] == ["native"]

    def test_providers_list(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "providers", "list"])
        assert code == 0 and any(item["id"] == "mock" for item in json.loads(out))

    def test_backends_list(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "backends", "list"])
        assert code == 0 and any(item["id"] == "native" for item in json.loads(out))

    def test_agents_list(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "agents", "list"])
        assert code == 0 and any(item["id"] == "coder" for item in json.loads(out))

    def test_agents_route_explains_the_decision(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "agents", "route", "테스트를 실행해줘"])
        assert code == 0
        ranking = json.loads(out)
        assert ranking and "score" in ranking[0]

    def test_tools_list(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "tools", "list"])
        names = {item["name"] for item in json.loads(out)}
        assert code == 0 and {"filesystem.read", "git.commit", "test.run"} <= names

    def test_plan(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "plan", "버그를 찾아서 고쳐줘"])
        assert code == 0 and json.loads(out)["tasks"]

    def test_config(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "config"])
        assert code == 0 and "routing" in json.loads(out)

    def test_doctor(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "doctor"])
        assert code in {0, 1} and "checks" in json.loads(out)

    def test_health(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "health"])
        assert code in {0, 1} and "providers" in json.loads(out)

    def test_tool_execution(self, capsys, workspace):
        code, out = self._run(
            capsys,
            ["--workspace", str(workspace), "tool", "filesystem.search", '{"pattern": "*.txt"}'],
        )
        assert code == 0 and json.loads(out)["output"] == ["notes.txt"]

    def test_tool_rejects_invalid_json(self, capsys, workspace):
        code, _ = self._run(capsys, ["--workspace", str(workspace), "tool", "system.info", "{oops"])
        assert code == 2

    def test_workflow_run(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "workflow", "run", "안녕하세요"])
        assert code == 0 and json.loads(out)["status"] == "completed"

    def test_chat(self, capsys, workspace):
        code, out = self._run(capsys, ["--workspace", str(workspace), "chat", "안녕하세요"])
        assert code == 0 and "Mock Provider" in out
