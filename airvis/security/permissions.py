"""Permission, risk and approval enforcement.

Every tool invocation passes through :meth:`PermissionManager.authorize`. There
is no bypass: the tool registry refuses to execute anything that was not
authorised, and high-risk operations can never be auto-approved unless the
policy explicitly says so.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..core.asyncutil import call_maybe_async
from ..core.config import SecurityConfig
from ..core.errors import ApprovalRequiredError, PermissionDeniedError
from ..core.events import EventBus, EventType
from ..tools.base import RiskLevel, Tool


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class ApprovalRequest:
    tool: str
    risk: RiskLevel
    arguments: dict[str, Any]
    reason: str
    agent_id: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    requested_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "risk": self.risk.name,
            "arguments": self.arguments,
            "reason": self.reason,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "requested_at": self.requested_at,
        }


@dataclass
class PermissionDecision:
    decision: Decision
    risk: RiskLevel
    tool: str
    reason: str = ""
    policy: str = "default"

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "risk": self.risk.name,
            "tool": self.tool,
            "reason": self.reason,
            "policy": self.policy,
        }


ApprovalHandler = Callable[[ApprovalRequest], bool | Awaitable[bool]]


class PermissionManager:
    """Applies risk levels, per-tool policies and workspace sandboxing."""

    def __init__(
        self,
        config: SecurityConfig | None = None,
        workspace: Path | str | None = None,
        *,
        event_bus: EventBus | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        self.config = config or SecurityConfig()
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.event_bus = event_bus
        self.approval_handler = approval_handler
        self._granted: set[str] = set()

    # -- risk ------------------------------------------------------------------

    def effective_risk(self, tool: Tool) -> RiskLevel:
        override = self.config.risk_overrides.get(tool.name)
        if override is not None:
            return RiskLevel.parse(override, tool.risk)
        return tool.risk

    @property
    def auto_approve_ceiling(self) -> RiskLevel:
        return RiskLevel.parse(self.config.auto_approve_max_risk, RiskLevel.LOW)

    # -- policy ----------------------------------------------------------------

    def evaluate(
        self,
        tool: Tool,
        arguments: dict[str, Any] | None = None,
        *,
        agent_permissions: set[str] | frozenset[str] | None = None,
        agent_tools: set[str] | frozenset[str] | None = None,
        confirm: bool = False,
    ) -> PermissionDecision:
        risk = self.effective_risk(tool)
        name = tool.name

        if name in set(self.config.denied_tools):
            return PermissionDecision(Decision.DENY, risk, name, "tool is on the deny list", "denied_tools")

        if agent_tools is not None and name not in agent_tools:
            return PermissionDecision(
                Decision.DENY, risk, name, "agent is not allowed to use this tool", "agent_tools"
            )

        missing = sorted(set(tool.required_permissions) - set(agent_permissions or ()) - self._granted)
        if missing:
            return PermissionDecision(
                Decision.DENY, risk, name, f"missing permission(s): {', '.join(missing)}", "permissions"
            )

        if tool.network and not self.config.allow_network:
            return PermissionDecision(Decision.DENY, risk, name, "network access is disabled", "network")

        policy = self.config.tool_policies.get(name)
        if policy:
            token = policy.strip().lower()
            if token == "deny":
                return PermissionDecision(Decision.DENY, risk, name, "denied by tool policy", "tool_policy")
            if token in {"allow", "auto", "automatic"}:
                return PermissionDecision(Decision.ALLOW, risk, name, "allowed by tool policy", "tool_policy")
            if token in {"approval", "approve", "ask"}:
                if confirm:
                    return PermissionDecision(Decision.ALLOW, risk, name, "confirmed by caller", "tool_policy")
                return PermissionDecision(
                    Decision.REQUIRE_APPROVAL, risk, name, "tool policy requires approval", "tool_policy"
                )

        if risk <= self.auto_approve_ceiling:
            return PermissionDecision(Decision.ALLOW, risk, name, "risk within auto-approval ceiling", "risk")

        default_policy = self.config.default_high_risk_policy.strip().lower()
        if default_policy == "deny":
            return PermissionDecision(
                Decision.DENY, risk, name, f"{risk.name} operations are denied by policy", "high_risk_policy"
            )
        if default_policy == "allow":
            return PermissionDecision(
                Decision.ALLOW, risk, name, f"{risk.name} auto-allowed by policy", "high_risk_policy"
            )
        if confirm:
            return PermissionDecision(Decision.ALLOW, risk, name, "confirmed by caller", "confirmation")
        return PermissionDecision(
            Decision.REQUIRE_APPROVAL, risk, name, f"{risk.name} operations require approval", "high_risk_policy"
        )

    async def authorize(
        self,
        tool: Tool,
        arguments: dict[str, Any] | None = None,
        *,
        agent_permissions: set[str] | frozenset[str] | None = None,
        agent_tools: set[str] | frozenset[str] | None = None,
        confirm: bool = False,
        approval_handler: ApprovalHandler | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
    ) -> PermissionDecision:
        """Raise unless the call may proceed; returns the winning decision."""
        arguments = arguments or {}
        decision = self.evaluate(
            tool,
            arguments,
            agent_permissions=agent_permissions,
            agent_tools=agent_tools,
            confirm=confirm,
        )

        if decision.decision is Decision.DENY:
            self._emit(EventType.TOOL_DENIED, tool, decision, workflow_id, task_id, agent_id)
            raise PermissionDeniedError(
                f"{tool.name} denied: {decision.reason}",
                tool=tool.name,
                risk=decision.risk.name,
                policy=decision.policy,
            )

        if decision.decision is Decision.REQUIRE_APPROVAL:
            handler = approval_handler or self.approval_handler
            request = ApprovalRequest(
                tool=tool.name,
                risk=decision.risk,
                arguments=arguments,
                reason=decision.reason,
                agent_id=agent_id,
                task_id=task_id,
                workflow_id=workflow_id,
            )
            self._emit(EventType.APPROVAL_REQUESTED, tool, decision, workflow_id, task_id, agent_id)
            if handler is None:
                raise ApprovalRequiredError(
                    f"{tool.name} requires approval: {decision.reason}",
                    tool=tool.name,
                    risk=decision.risk.name,
                    policy=decision.policy,
                )
            granted = bool(await call_maybe_async(handler, request))
            if not granted:
                self._emit(EventType.TOOL_DENIED, tool, decision, workflow_id, task_id, agent_id)
                raise PermissionDeniedError(
                    f"{tool.name} was not approved",
                    tool=tool.name,
                    risk=decision.risk.name,
                    policy="approval_handler",
                )
            decision = PermissionDecision(Decision.ALLOW, decision.risk, tool.name, "approved", "approval_handler")

        return decision

    # -- sandboxing ------------------------------------------------------------

    def resolve_path(self, path: str | Path, *, must_exist: bool = False) -> Path:
        """Resolve ``path`` inside the workspace sandbox."""
        candidate = Path(path)
        candidate = (self.workspace / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if self.config.workspace_restricted and not self._within_allowed_roots(candidate):
            raise PermissionDeniedError(
                f"path escapes the AIRVIS workspace: {path}", path=str(path), workspace=str(self.workspace)
            )
        if must_exist and not candidate.exists():
            raise FileNotFoundError(str(path))
        return candidate

    def _within_allowed_roots(self, candidate: Path) -> bool:
        roots = [self.workspace, *(Path(item).expanduser().resolve() for item in self.config.additional_writable_paths)]
        return any(candidate == root or root in candidate.parents for root in roots)

    # -- runtime grants --------------------------------------------------------

    def grant(self, *permissions: str) -> None:
        self._granted.update(permissions)

    def revoke(self, *permissions: str) -> None:
        self._granted.difference_update(permissions)

    @property
    def granted(self) -> frozenset[str]:
        return frozenset(self._granted)

    def describe(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "auto_approve_max_risk": self.auto_approve_ceiling.name,
            "default_high_risk_policy": self.config.default_high_risk_policy,
            "workspace_restricted": self.config.workspace_restricted,
            "allow_network": self.config.allow_network,
            "denied_tools": list(self.config.denied_tools),
            "tool_policies": dict(self.config.tool_policies),
            "risk_overrides": dict(self.config.risk_overrides),
            "granted": sorted(self._granted),
        }

    def _emit(
        self,
        event_type: EventType,
        tool: Tool,
        decision: PermissionDecision,
        workflow_id: str | None,
        task_id: str | None,
        agent_id: str | None,
    ) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(
            event_type,
            tool=tool.name,
            workflow_id=workflow_id,
            task_id=task_id,
            agent_id=agent_id,
            status=decision.decision.value,
            metadata=decision.to_dict(),
        )


def always_approve(_: ApprovalRequest) -> bool:
    """Approval handler for trusted automation (tests, headless runs)."""
    return True


def never_approve(_: ApprovalRequest) -> bool:
    return False


__all__ = [
    "ApprovalHandler",
    "ApprovalRequest",
    "Decision",
    "PermissionDecision",
    "PermissionManager",
    "always_approve",
    "never_approve",
]
