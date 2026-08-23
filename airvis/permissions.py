"""Deprecated module kept for backward compatibility.

The canonical implementations live in :mod:`airvis.security.permissions` and
:mod:`airvis.tools`.
"""

from __future__ import annotations

from .core.errors import ApprovalRequiredError, PermissionDeniedError
from .security.permissions import (
    ApprovalRequest,
    Decision,
    PermissionDecision,
    PermissionManager,
    always_approve,
    never_approve,
)
from .tools.base import RiskLevel
from .tools.registry import ToolRegistry, command_risk

#: V4 alias.
PermissionError = PermissionDeniedError

__all__ = [
    "ApprovalRequest",
    "ApprovalRequiredError",
    "Decision",
    "PermissionDecision",
    "PermissionError",
    "PermissionManager",
    "RiskLevel",
    "ToolRegistry",
    "always_approve",
    "command_risk",
    "never_approve",
]
