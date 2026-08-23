"""Structured exception hierarchy for AIRVIS.

Every failure raised inside the orchestration pipeline is an :class:`AirvisError`
so the error analyser can classify it without inspecting message strings.
"""

from __future__ import annotations

from typing import Any


class AirvisError(Exception):
    """Base class for every AIRVIS failure."""

    code = "airvis_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


# --- configuration -----------------------------------------------------------


class ConfigError(AirvisError):
    code = "config_error"


# --- registries --------------------------------------------------------------


class RegistryError(AirvisError):
    code = "registry_error"


class DuplicateRegistrationError(RegistryError):
    code = "duplicate_registration"


class UnknownProviderError(RegistryError):
    code = "unknown_provider"


class UnknownBackendError(RegistryError):
    code = "unknown_backend"


class UnknownAgentError(RegistryError):
    code = "unknown_agent"


class UnknownToolError(RegistryError):
    code = "unknown_tool"


class InvalidReferenceError(RegistryError):
    """An agent points at a backend/provider/tool that does not exist."""

    code = "invalid_reference"


class NoAgentAvailableError(RegistryError):
    code = "no_agent_available"


# --- providers ---------------------------------------------------------------


class ProviderError(AirvisError):
    code = "provider_error"


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"


class RateLimitError(ProviderError):
    code = "rate_limit"


class CapabilityError(ProviderError):
    """The provider does not implement a requested capability."""

    code = "capability_unsupported"


# --- backends ----------------------------------------------------------------


class BackendError(AirvisError):
    code = "backend_error"


class BackendUnavailableError(BackendError):
    code = "backend_unavailable"


class BackendTimeoutError(BackendError):
    code = "backend_timeout"


# --- tools & security --------------------------------------------------------


class ToolError(AirvisError):
    code = "tool_error"


class ToolExecutionError(ToolError):
    code = "tool_execution_error"


class ToolTimeoutError(ToolError):
    code = "tool_timeout"


class PermissionDeniedError(AirvisError):
    code = "permission_denied"


class ApprovalRequiredError(PermissionDeniedError):
    """A human (or policy) must approve before the call may proceed."""

    code = "approval_required"


# --- orchestration -----------------------------------------------------------


class PlanningError(AirvisError):
    code = "planning_error"


class DAGError(AirvisError):
    code = "dag_error"


class DAGCycleError(DAGError):
    code = "dag_cycle"


class TaskTimeoutError(AirvisError):
    code = "task_timeout"


class TaskCancelledError(AirvisError):
    code = "task_cancelled"


class WorkflowCancelledError(AirvisError):
    code = "workflow_cancelled"


class ReviewRejectedError(AirvisError):
    """A reviewer refused the output of a task."""

    code = "review_rejected"


class RepairExhaustedError(AirvisError):
    code = "repair_exhausted"


class ContextError(AirvisError):
    code = "context_error"


class ArtifactError(AirvisError):
    code = "artifact_error"


__all__ = [name for name in dir() if name.endswith("Error")]
