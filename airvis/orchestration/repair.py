"""Adaptive repair: classify the failure, then pick a bounded strategy.

The planner is policy-driven and explicitly loop-safe — a strategy is never
tried twice for the same task, and every path terminates in ABORT.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core import errors as err
from ..core.config import RepairConfig
from .review import ReviewResult
from .task import Task, TaskResult


class FailureCategory(str, Enum):
    PROVIDER_ERROR = "PROVIDER_ERROR"
    BACKEND_ERROR = "BACKEND_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    CODE_ERROR = "CODE_ERROR"
    TEST_FAILURE = "TEST_FAILURE"
    CONTEXT_ERROR = "CONTEXT_ERROR"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    ROUTING_ERROR = "ROUTING_ERROR"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class RepairStrategy(str, Enum):
    RETRY = "RETRY"
    REPLAN = "REPLAN"
    CHANGE_AGENT = "CHANGE_AGENT"
    CHANGE_PROVIDER = "CHANGE_PROVIDER"
    CHANGE_MODEL = "CHANGE_MODEL"
    CHANGE_BACKEND = "CHANGE_BACKEND"
    MODIFY_CONTEXT = "MODIFY_CONTEXT"
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    ABORT = "ABORT"


#: category -> ordered escalation path. Every path ends in ABORT.
DEFAULT_PLAYBOOK: dict[FailureCategory, list[RepairStrategy]] = {
    FailureCategory.PROVIDER_ERROR: [RepairStrategy.RETRY, RepairStrategy.CHANGE_PROVIDER,
                                     RepairStrategy.CHANGE_MODEL, RepairStrategy.ABORT],
    FailureCategory.RATE_LIMIT: [RepairStrategy.CHANGE_PROVIDER, RepairStrategy.RETRY, RepairStrategy.ABORT],
    FailureCategory.BACKEND_ERROR: [RepairStrategy.CHANGE_BACKEND, RepairStrategy.CHANGE_AGENT,
                                    RepairStrategy.RETRY, RepairStrategy.ABORT],
    FailureCategory.TOOL_ERROR: [RepairStrategy.RETRY, RepairStrategy.MODIFY_CONTEXT,
                                 RepairStrategy.CHANGE_AGENT, RepairStrategy.ABORT],
    FailureCategory.PERMISSION_ERROR: [RepairStrategy.REQUEST_APPROVAL, RepairStrategy.CHANGE_AGENT,
                                       RepairStrategy.HUMAN_REVIEW, RepairStrategy.ABORT],
    FailureCategory.CODE_ERROR: [RepairStrategy.MODIFY_CONTEXT, RepairStrategy.REPLAN,
                                 RepairStrategy.CHANGE_AGENT, RepairStrategy.ABORT],
    FailureCategory.TEST_FAILURE: [RepairStrategy.MODIFY_CONTEXT, RepairStrategy.REPLAN,
                                   RepairStrategy.HUMAN_REVIEW, RepairStrategy.ABORT],
    FailureCategory.CONTEXT_ERROR: [RepairStrategy.MODIFY_CONTEXT, RepairStrategy.REPLAN, RepairStrategy.ABORT],
    FailureCategory.TIMEOUT: [RepairStrategy.RETRY, RepairStrategy.REPLAN, RepairStrategy.CHANGE_BACKEND,
                              RepairStrategy.ABORT],
    FailureCategory.ROUTING_ERROR: [RepairStrategy.MODIFY_CONTEXT, RepairStrategy.HUMAN_REVIEW,
                                    RepairStrategy.ABORT],
    FailureCategory.REVIEW_REJECTED: [RepairStrategy.MODIFY_CONTEXT, RepairStrategy.CHANGE_AGENT,
                                      RepairStrategy.REPLAN, RepairStrategy.HUMAN_REVIEW, RepairStrategy.ABORT],
    FailureCategory.CANCELLED: [RepairStrategy.ABORT],
    FailureCategory.UNKNOWN: [RepairStrategy.RETRY, RepairStrategy.CHANGE_AGENT, RepairStrategy.ABORT],
}

_EXCEPTION_MAP: list[tuple[type[Exception], FailureCategory]] = [
    (err.RateLimitError, FailureCategory.RATE_LIMIT),
    (err.ProviderTimeoutError, FailureCategory.TIMEOUT),
    (err.ProviderError, FailureCategory.PROVIDER_ERROR),
    (err.BackendTimeoutError, FailureCategory.TIMEOUT),
    (err.BackendError, FailureCategory.BACKEND_ERROR),
    (err.ToolTimeoutError, FailureCategory.TIMEOUT),
    (err.PermissionDeniedError, FailureCategory.PERMISSION_ERROR),
    (err.ToolError, FailureCategory.TOOL_ERROR),
    (err.TaskTimeoutError, FailureCategory.TIMEOUT),
    (err.TaskCancelledError, FailureCategory.CANCELLED),
    (err.WorkflowCancelledError, FailureCategory.CANCELLED),
    (err.ReviewRejectedError, FailureCategory.REVIEW_REJECTED),
    (err.ContextError, FailureCategory.CONTEXT_ERROR),
    (err.NoAgentAvailableError, FailureCategory.ROUTING_ERROR),
    (err.UnknownAgentError, FailureCategory.ROUTING_ERROR),
    (err.UnknownBackendError, FailureCategory.ROUTING_ERROR),
    (err.UnknownProviderError, FailureCategory.ROUTING_ERROR),
    (err.InvalidReferenceError, FailureCategory.ROUTING_ERROR),
    (err.PlanningError, FailureCategory.CONTEXT_ERROR),
    (err.DAGError, FailureCategory.CONTEXT_ERROR),
]


@dataclass
class FailureAnalysis:
    category: FailureCategory
    detail: str
    code: str = ""
    retryable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "detail": self.detail,
            "code": self.code,
            "retryable": self.retryable,
            "metadata": self.metadata,
        }


@dataclass
class RepairDecision:
    strategy: RepairStrategy
    reason: str
    analysis: FailureAnalysis
    delay_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    decided_at: float = field(default_factory=time.time)

    @property
    def gives_up(self) -> bool:
        return self.strategy in {RepairStrategy.ABORT, RepairStrategy.HUMAN_REVIEW}

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "reason": self.reason,
            "analysis": self.analysis.to_dict(),
            "delay_seconds": self.delay_seconds,
            "metadata": self.metadata,
            "decided_at": self.decided_at,
        }


class ErrorAnalyzer:
    """Maps exceptions and failed results onto a failure category."""

    def classify_exception(self, exc: BaseException) -> FailureAnalysis:
        import asyncio

        if isinstance(exc, asyncio.TimeoutError):
            return FailureAnalysis(FailureCategory.TIMEOUT, "operation timed out", "timeout")
        if isinstance(exc, asyncio.CancelledError):
            return FailureAnalysis(FailureCategory.CANCELLED, "cancelled", "cancelled", retryable=False)
        for exception_type, category in _EXCEPTION_MAP:
            if isinstance(exc, exception_type):
                return FailureAnalysis(
                    category,
                    str(exc),
                    getattr(exc, "code", exception_type.__name__),
                    retryable=category is not FailureCategory.CANCELLED,
                    metadata=dict(getattr(exc, "details", {}) or {}),
                )
        if isinstance(exc, (SyntaxError, NameError, AttributeError, TypeError, ValueError, KeyError, IndexError)):
            return FailureAnalysis(FailureCategory.CODE_ERROR, f"{type(exc).__name__}: {exc}", type(exc).__name__)
        return FailureAnalysis(FailureCategory.UNKNOWN, f"{type(exc).__name__}: {exc}", type(exc).__name__)

    def classify_result(self, result: TaskResult) -> FailureAnalysis:
        code = (result.error_code or "").lower()
        detail = result.error or "task reported failure"
        mapping = {
            "permission_denied": FailureCategory.PERMISSION_ERROR,
            "approval_required": FailureCategory.PERMISSION_ERROR,
            "tool_error": FailureCategory.TOOL_ERROR,
            "tool_execution_error": FailureCategory.TOOL_ERROR,
            "tool_timeout": FailureCategory.TIMEOUT,
            "provider_error": FailureCategory.PROVIDER_ERROR,
            "provider_unavailable": FailureCategory.PROVIDER_ERROR,
            "provider_timeout": FailureCategory.TIMEOUT,
            "rate_limit": FailureCategory.RATE_LIMIT,
            "backend_error": FailureCategory.BACKEND_ERROR,
            "backend_unavailable": FailureCategory.BACKEND_ERROR,
            "backend_timeout": FailureCategory.TIMEOUT,
            "task_timeout": FailureCategory.TIMEOUT,
            "task_cancelled": FailureCategory.CANCELLED,
            "upstream_failed": FailureCategory.CANCELLED,
            "review_rejected": FailureCategory.REVIEW_REJECTED,
            "no_agent_available": FailureCategory.ROUTING_ERROR,
        }
        category = mapping.get(code, FailureCategory.UNKNOWN)
        if category is FailureCategory.UNKNOWN and any(
            not item.get("ok") and str(item.get("tool")) == "test.run" for item in result.tool_results
        ):
            category = FailureCategory.TEST_FAILURE
        return FailureAnalysis(
            category, detail, result.error_code or "", retryable=category is not FailureCategory.CANCELLED
        )

    def classify_review(self, review: ReviewResult) -> FailureAnalysis:
        blocking = review.blocking_issues()
        if any(issue.dimension == "tests" for issue in blocking):
            category = FailureCategory.TEST_FAILURE
        elif any(issue.dimension == "security" for issue in blocking):
            category = FailureCategory.PERMISSION_ERROR
        else:
            category = FailureCategory.REVIEW_REJECTED
        detail = "; ".join(issue.message for issue in blocking) or "review rejected the output"
        return FailureAnalysis(
            category, detail, "review_rejected", metadata={"score": review.score, "issues": len(review.issues)}
        )


class RepairPlanner:
    """Chooses the next repair strategy under configured bounds."""

    def __init__(self, config: RepairConfig | None = None) -> None:
        self.config = config or RepairConfig()

    def playbook(self, category: FailureCategory) -> list[RepairStrategy]:
        override = self.config.strategies.get(category.value)
        if override:
            strategies: list[RepairStrategy] = []
            for name in override:
                try:
                    strategies.append(RepairStrategy(str(name).strip().upper()))
                except ValueError:
                    continue
            if strategies and strategies[-1] is not RepairStrategy.ABORT:
                strategies.append(RepairStrategy.ABORT)
            if strategies:
                return strategies
        return DEFAULT_PLAYBOOK.get(category, DEFAULT_PLAYBOOK[FailureCategory.UNKNOWN])

    def plan(
        self,
        task: Task,
        analysis: FailureAnalysis,
        *,
        workflow_repairs: int = 0,
        has_approval_handler: bool = False,
    ) -> RepairDecision:
        if not analysis.retryable:
            return RepairDecision(RepairStrategy.ABORT, "failure is not retryable", analysis)
        if task.repair_attempts >= self.config.max_repairs_per_task:
            return RepairDecision(
                RepairStrategy.ABORT,
                f"task repair budget exhausted ({self.config.max_repairs_per_task})",
                analysis,
            )
        if workflow_repairs >= self.config.max_repairs_per_workflow:
            return RepairDecision(
                RepairStrategy.ABORT,
                f"workflow repair budget exhausted ({self.config.max_repairs_per_workflow})",
                analysis,
            )

        attempted = set(task.attempted_repairs)
        for strategy in self.playbook(analysis.category):
            if strategy.value in attempted:
                continue
            if strategy is RepairStrategy.RETRY and task.attempts >= min(
                task.retry_policy.max_attempts, self.config.max_retries
            ):
                continue
            if strategy is RepairStrategy.REQUEST_APPROVAL and not has_approval_handler:
                continue
            if strategy is RepairStrategy.HUMAN_REVIEW and not self.config.allow_human_review:
                continue
            delay = task.retry_policy.delay_for(task.attempts) if strategy is RepairStrategy.RETRY else 0.0
            if strategy is RepairStrategy.RETRY and analysis.category is FailureCategory.RATE_LIMIT:
                delay = max(delay, self.config.retry_backoff_seconds * 4)
            return RepairDecision(
                strategy,
                f"{analysis.category.value} -> {strategy.value} (attempt {task.repair_attempts + 1})",
                analysis,
                delay_seconds=delay,
            )

        return RepairDecision(RepairStrategy.ABORT, "every repair strategy for this category was already tried",
                              analysis)


__all__ = [
    "DEFAULT_PLAYBOOK",
    "ErrorAnalyzer",
    "FailureAnalysis",
    "FailureCategory",
    "RepairDecision",
    "RepairPlanner",
    "RepairStrategy",
]
