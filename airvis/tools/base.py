"""The single canonical Tool abstraction used by every AIRVIS subsystem."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import ToolExecutionError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..security.permissions import PermissionManager


class RiskLevel(IntEnum):
    """Ordered risk scale; ordering is what the approval policy compares."""

    SAFE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value: RiskLevel | str | int | None, default: RiskLevel | None = None) -> RiskLevel:
        if isinstance(value, RiskLevel):
            return value
        if isinstance(value, int):
            return cls(max(0, min(4, value)))
        if isinstance(value, str):
            token = value.strip().upper()
            if token in cls.__members__:
                return cls[token]
            legacy = _LEGACY_RISK.get(token)
            if legacy is not None:
                return legacy
        if default is not None:
            return default
        raise ValueError(f"unknown risk level: {value!r}")

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


#: Risk vocabulary used by AIRVIS V4, mapped onto the V6 scale.
_LEGACY_RISK: dict[str, RiskLevel] = {
    "READ": RiskLevel.SAFE,
    "NETWORK": RiskLevel.LOW,
    "MODIFY": RiskLevel.MEDIUM,
    "WRITE": RiskLevel.MEDIUM,
    "DESTRUCTIVE": RiskLevel.HIGH,
}


@dataclass
class ToolContext:
    """Everything a tool is allowed to know about its call site."""

    workspace: Path
    permissions: PermissionManager | None = None
    workflow_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    timeout: float = 60.0
    allow_network: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def resolve_path(self, path: str, *, must_exist: bool = False) -> Path:
        if self.permissions is not None:
            return self.permissions.resolve_path(path, must_exist=must_exist)
        candidate = (self.workspace / path).resolve()
        if must_exist and not candidate.exists():
            raise FileNotFoundError(path)
        return candidate


@dataclass
class ToolResult:
    """Structured outcome of a tool invocation."""

    tool: str
    ok: bool = True
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    #: artifact descriptors ({"type", "name", "path"|"content", "metadata"})
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "metadata": self.metadata,
            "artifacts": self.artifacts,
        }

    def unwrap(self) -> Any:
        if not self.ok:
            raise ToolExecutionError(self.error or f"{self.tool} failed", tool=self.tool)
        return self.output


class Tool:
    """Base class for every executable capability.

    Subclasses set the class attributes and implement :meth:`run`.
    """

    name: str = ""
    description: str = ""
    risk: RiskLevel = RiskLevel.SAFE
    required_permissions: frozenset[str] = frozenset()
    #: JSON-schema style parameter description
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    #: tools that reach the network are skipped when networking is disabled
    network: bool = False
    tags: frozenset[str] = frozenset()

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            setattr(self, key, value)
        if not self.name:
            raise ValueError(f"{type(self).__name__} must define a name")

    async def run(self, context: ToolContext, **arguments: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError(f"{self.name} does not implement run()")

    # -- introspection ---------------------------------------------------------

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.name,
            "required_permissions": sorted(self.required_permissions),
            "parameters": self.parameters,
            "network": self.network,
            "tags": sorted(self.tags),
        }

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Reject unknown/missing arguments before anything is executed."""
        properties = self.parameters.get("properties", {}) if isinstance(self.parameters, dict) else {}
        required = self.parameters.get("required", []) if isinstance(self.parameters, dict) else []
        if properties:
            unknown = sorted(set(arguments) - set(properties))
            if unknown:
                raise ToolExecutionError(
                    f"{self.name}: unknown argument(s): {', '.join(unknown)}", tool=self.name
                )
        missing = [key for key in required if key not in arguments]
        if missing:
            raise ToolExecutionError(
                f"{self.name}: missing required argument(s): {', '.join(missing)}", tool=self.name
            )
        return arguments

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Tool {self.name} risk={self.risk.name}>"


class FunctionTool(Tool):
    """Adapter that turns a plain callable into a :class:`Tool`.

    The positional signature matches the AIRVIS V4 ``Tool`` dataclass so plugins
    written against ``airvis.sdk`` keep working unchanged.
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        risk: RiskLevel | str = RiskLevel.SAFE,
        handler: Callable[..., Any] | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        required_permissions: set[str] | frozenset[str] | None = None,
        network: bool = False,
        tags: set[str] | frozenset[str] | None = None,
    ) -> None:
        if handler is None:
            raise ValueError(f"FunctionTool {name!r} requires a handler")
        self.name = name
        self.description = description
        self.risk = RiskLevel.parse(risk, RiskLevel.MEDIUM)
        self.handler = handler
        self.parameters = parameters or _infer_parameters(handler)
        self.required_permissions = frozenset(required_permissions or ())
        self.network = network
        self.tags = frozenset(tags or ())
        self._wants_context = "context" in inspect.signature(handler).parameters

    async def run(self, context: ToolContext, **arguments: Any) -> Any:
        if self._wants_context:
            arguments = {**arguments, "context": context}
        result = self.handler(**arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    @property
    def _handler(self) -> Callable[..., Any]:  # pragma: no cover - legacy alias
        return self.handler


def _infer_parameters(handler: Callable[..., Any]) -> dict[str, Any]:
    """Derive a minimal JSON schema from a callable signature."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return {"type": "object", "properties": {}, "required": []}
    for name, parameter in signature.parameters.items():
        if name in {"self", "context"} or parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        properties[name] = {"type": _json_type(parameter.annotation)}
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def _json_type(annotation: Any) -> str:
    mapping = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
    return mapping.get(annotation, "string")


__all__ = ["FunctionTool", "RiskLevel", "Tool", "ToolContext", "ToolResult"]
