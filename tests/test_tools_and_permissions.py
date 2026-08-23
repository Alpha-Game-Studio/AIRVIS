"""ToolRegistry and PermissionManager unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from airvis.core.errors import (
    ApprovalRequiredError,
    DuplicateRegistrationError,
    PermissionDeniedError,
    ToolExecutionError,
    ToolTimeoutError,
    UnknownToolError,
)
from airvis.security.permissions import Decision, PermissionManager, always_approve, never_approve
from airvis.tools.base import FunctionTool, RiskLevel, Tool, ToolContext
from airvis.tools.code import analyze_source
from airvis.tools.registry import ToolRegistry, command_risk
from airvis.tools.terminal import classify_command


class TestRiskLevel:
    def test_orders_by_severity(self):
        assert RiskLevel.SAFE < RiskLevel.LOW < RiskLevel.MEDIUM < RiskLevel.HIGH < RiskLevel.CRITICAL

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("HIGH", RiskLevel.HIGH),
            ("critical", RiskLevel.CRITICAL),
            ("READ", RiskLevel.SAFE),
            ("MODIFY", RiskLevel.MEDIUM),
            ("DESTRUCTIVE", RiskLevel.HIGH),
            ("NETWORK", RiskLevel.LOW),
            (3, RiskLevel.HIGH),
        ],
    )
    def test_parses_current_and_legacy_names(self, value, expected):
        assert RiskLevel.parse(value) is expected

    def test_unknown_without_default_raises(self):
        with pytest.raises(ValueError):
            RiskLevel.parse("nonsense")


class TestToolRegistry:
    def test_builtins_are_registered(self, tools: ToolRegistry):
        for name in ("filesystem.read", "filesystem.write", "filesystem.delete", "terminal.execute",
                     "git.clone", "git.diff", "git.commit", "git.push", "web.fetch",
                     "code.analyze", "test.run"):
            assert tools.has(name), name

    def test_get_unknown_raises(self, tools: ToolRegistry):
        with pytest.raises(UnknownToolError):
            tools.get("does.not.exist")

    def test_duplicate_registration_can_be_rejected(self, tools: ToolRegistry):
        tool = FunctionTool("custom.echo", "echo", RiskLevel.SAFE, lambda text: text)
        tools.register(tool)
        with pytest.raises(DuplicateRegistrationError):
            tools.register(tool, replace=False)

    def test_schemas_expose_the_required_fields(self, tools: ToolRegistry):
        schema = tools.get("filesystem.write").schema()
        assert set(schema) >= {"name", "description", "risk", "required_permissions", "parameters"}

    async def test_unknown_argument_is_rejected(self, tools: ToolRegistry):
        with pytest.raises(ToolExecutionError):
            await tools.call("filesystem.read", {"path": "notes.txt", "bogus": 1})

    async def test_missing_argument_is_rejected(self, tools: ToolRegistry):
        with pytest.raises(ToolExecutionError):
            await tools.call("filesystem.read", {})

    async def test_safe_tool_runs_without_approval(self, tools: ToolRegistry):
        result = await tools.call("filesystem.search", {"pattern": "*.txt"})
        assert result.ok and result.output == ["notes.txt"]

    async def test_function_tool_adapter_executes(self, tools: ToolRegistry):
        tools.register(FunctionTool("custom.double", "double", RiskLevel.SAFE, lambda value: value * 2))
        result = await tools.call("custom.double", {"value": 21})
        assert result.output == 42

    async def test_timeout_is_reported_as_tool_timeout(self, tools: ToolRegistry):
        async def slow() -> str:
            import asyncio

            await asyncio.sleep(5)
            return "never"

        tools.register(FunctionTool("custom.slow", "slow", RiskLevel.SAFE, slow))
        with pytest.raises(ToolTimeoutError):
            await tools.call("custom.slow", {}, timeout=0.05)

    def test_sync_execute_wrapper_returns_raw_output(self, tools: ToolRegistry):
        assert tools.execute("filesystem.search", {"pattern": "*.txt"}) == ["notes.txt"]


class TestFilesystemSandbox:
    async def test_read_outside_workspace_is_denied(self, tools: ToolRegistry):
        with pytest.raises(PermissionDeniedError):
            await tools.call("filesystem.read", {"path": "../escape.txt"})

    async def test_absolute_path_outside_workspace_is_denied(self, tools: ToolRegistry):
        with pytest.raises(PermissionDeniedError):
            await tools.call("filesystem.read", {"path": "/etc/passwd"})

    async def test_write_then_read_round_trip(self, tools: ToolRegistry, workspace: Path):
        written = await tools.call(
            "filesystem.write", {"path": "out/report.md", "content": "hello"}, confirm=True,
            agent_permissions={"filesystem.write"},
        )
        assert (workspace / "out" / "report.md").read_text(encoding="utf-8") == "hello"
        assert written.artifacts and written.artifacts[0]["type"] == "file"

    async def test_write_requires_permission(self, tools: ToolRegistry):
        with pytest.raises(PermissionDeniedError):
            await tools.call("filesystem.write", {"path": "x.txt", "content": "y"}, confirm=True)


class TestPermissionManager:
    def test_risk_within_ceiling_is_auto_allowed(self, tools, permissions):
        decision = permissions.evaluate(tools.get("filesystem.read"))
        assert decision.decision is Decision.ALLOW

    def test_high_risk_requires_approval_by_default(self, tools, permissions):
        decision = permissions.evaluate(
            tools.get("terminal.execute"), agent_permissions={"terminal.execute"}
        )
        assert decision.decision is Decision.REQUIRE_APPROVAL

    def test_deny_list_wins_over_everything(self, tools, config, workspace):
        config.security.denied_tools = ["filesystem.read"]
        manager = PermissionManager(config.security, workspace)
        assert manager.evaluate(tools.get("filesystem.read")).decision is Decision.DENY

    def test_risk_override_is_applied(self, tools, config, workspace):
        config.security.risk_overrides = {"filesystem.read": "CRITICAL"}
        manager = PermissionManager(config.security, workspace)
        assert manager.effective_risk(tools.get("filesystem.read")) is RiskLevel.CRITICAL
        assert manager.evaluate(tools.get("filesystem.read")).decision is Decision.REQUIRE_APPROVAL

    def test_agent_tool_whitelist_is_enforced(self, tools, permissions):
        decision = permissions.evaluate(tools.get("filesystem.read"), agent_tools={"git.status"})
        assert decision.decision is Decision.DENY

    def test_network_can_be_disabled_globally(self, tools, config, workspace):
        config.security.allow_network = False
        manager = PermissionManager(config.security, workspace)
        decision = manager.evaluate(tools.get("web.fetch"), agent_permissions={"network"})
        assert decision.decision is Decision.DENY

    async def test_approval_handler_can_grant(self, tools, permissions):
        decision = await permissions.authorize(
            tools.get("terminal.execute"),
            {"command": "pwd"},
            agent_permissions={"terminal.execute"},
            approval_handler=always_approve,
        )
        assert decision.allowed

    async def test_approval_handler_can_refuse(self, tools, permissions):
        with pytest.raises(PermissionDeniedError):
            await permissions.authorize(
                tools.get("terminal.execute"),
                {"command": "pwd"},
                agent_permissions={"terminal.execute"},
                approval_handler=never_approve,
            )

    async def test_missing_handler_raises_approval_required(self, tools, permissions):
        with pytest.raises(ApprovalRequiredError):
            await permissions.authorize(
                tools.get("terminal.execute"), {"command": "pwd"}, agent_permissions={"terminal.execute"}
            )

    async def test_critical_command_cannot_inherit_high_approval(self, tools: ToolRegistry):
        with pytest.raises(PermissionDeniedError):
            await tools.call(
                "terminal.execute",
                {"command": "sudo rm -rf /"},
                confirm=True,
                agent_permissions={"terminal.execute"},
                approval_handler=always_approve,
            )


class TestTerminalClassification:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("pwd", RiskLevel.SAFE),
            ("git status", RiskLevel.SAFE),
            ("pip install requests", RiskLevel.MEDIUM),
            ("rm -f build", RiskLevel.HIGH),
            ("git push origin main", RiskLevel.HIGH),
            ("sudo reboot", RiskLevel.CRITICAL),
        ],
    )
    def test_classification(self, command, expected):
        assert classify_command(command) is expected

    def test_legacy_command_risk_returns_a_name(self):
        assert command_risk("sudo shutdown") == "CRITICAL"


class TestCodeAnalysis:
    def test_detects_unreachable_code_after_return(self):
        findings = analyze_source("def f():\n    return 1\n    x = 2\n", "f.py")
        assert any(item.rule == "unreachable-code" for item in findings)

    def test_detects_global_without_module_binding(self):
        source = "def f():\n    global missing\n    if missing is None:\n        pass\n"
        findings = analyze_source(source, "f.py")
        assert any(item.rule == "global-without-module-binding" for item in findings)

    def test_accepts_a_correctly_bound_global(self):
        source = "missing = None\n\n\ndef f():\n    global missing\n    missing = 1\n"
        findings = analyze_source(source, "f.py")
        assert not any(item.rule == "global-without-module-binding" for item in findings)

    def test_syntax_error_becomes_a_critical_finding(self):
        findings = analyze_source("def broken(:\n", "f.py")
        assert findings and findings[0].severity == "critical"

    def test_detects_silent_exception_swallowing(self):
        source = "def f():\n    try:\n        pass\n    except Exception:\n        pass\n"
        assert any(item.rule == "silent-exception" for item in analyze_source(source, "f.py"))


class TestToolContext:
    def test_resolve_path_uses_the_permission_manager(self, tools: ToolRegistry, workspace: Path):
        context: ToolContext = tools.context()
        assert context.resolve_path("notes.txt") == (workspace / "notes.txt").resolve()

    def test_custom_tool_subclass_requires_a_name(self):
        class Nameless(Tool):
            pass

        with pytest.raises(ValueError):
            Nameless()
