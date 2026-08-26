"""The single canonical Tool abstraction used by every AIRVIS subsystem."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import ToolExecutionError

if TYPE_CHECKING:
    from ..security.permissions import PermissionManager


class RiskLevel(IntEnum):
    SAFE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value: "RiskLevel | str | int | None", default: "RiskLevel | None" = None) -> "RiskLevel":
        if isinstance(value, RiskLevel): return value
        if isinstance(value, int): return cls(max(0, min(4, value)))
        if isinstance(value, str):
            token = value.strip().upper()
            if token in cls.__members__: return cls[token]
            legacy = {"READ": cls.SAFE, "NETWORK": cls.LOW, "MODIFY": cls.MEDIUM,
                      "WRITE": cls.MEDIUM, "DESTRUCTIVE": cls.HIGH}.get(token)
            if legacy is not None: return legacy
        if default is not None: return default
        raise ValueError(f"unknown risk level: {value!r}")

    def __str__(self) -> str: return self.name


_LEGACY_RISK: dict[str, RiskLevel] = {
    "READ": RiskLevel.SAFE, "NETWORK": RiskLevel.LOW, "MODIFY": RiskLevel.MEDIUM,
    "WRITE": RiskLevel.MEDIUM, "DESTRUCTIVE": RiskLevel.HIGH,
}


@dataclass
class ToolContext:
    workspace: Path
    permissions: PermissionManager | None = None
    workflow_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    timeout: float = 60.0
    allow_network: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def resolve_path(self, path: str, *, must_exist: bool = False) -> Path:
        if self.permissions is not None: return self.permissions.resolve_path(path, must_exist=must_exist)
        candidate = (self.workspace / path).resolve()
        if must_exist and not candidate.exists(): raise FileNotFoundError(path)
        return candidate


@dataclass
class ToolResult:
    tool: str
    ok: bool = True
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "ok": self.ok, "output": self.output, "error": self.error,
                "duration_ms": round(self.duration_ms, 2), "metadata": self.metadata, "artifacts": self.artifacts}

    def unwrap(self) -> Any:
        if not self.ok: raise ToolExecutionError(self.error or f"{self.tool} failed", tool=self.tool)
        return self.output


def provider_safe_name(name: str) -> str:
    """Convert a human/tool name to the OpenAI function-name grammar."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(name))
    safe = re.sub(r"_+", "_", safe).strip("_") or "tool"
    return safe[:64]


class Tool:
    name: str = ""
    description: str = ""
    risk: RiskLevel = RiskLevel.SAFE
    required_permissions: frozenset[str] = frozenset()
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    network: bool = False
    tags: frozenset[str] = frozenset()

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items(): setattr(self, key, value)
        if not self.name: raise ValueError(f"{type(self).__name__} must define a name")

    async def run(self, context: ToolContext, **arguments: Any) -> Any:
        raise NotImplementedError(f"{self.name} does not implement run()")

    def schema(self) -> dict[str, Any]:
        # Keep the canonical name for AIRVIS routing while exposing a provider-safe
        # name to OpenAI/OpenRouter-compatible tool schemas.
        return {"name": provider_safe_name(self.name), "canonical_name": self.name,
                "description": self.description, "risk": self.risk.name,
                "required_permissions": sorted(self.required_permissions), "parameters": self.parameters,
                "network": self.network, "tags": sorted(self.tags)}

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        properties = self.parameters.get("properties", {}) if isinstance(self.parameters, dict) else {}
        required = self.parameters.get("required", []) if isinstance(self.parameters, dict) else []
        if properties:
            unknown = sorted(set(arguments) - set(properties))
            if unknown: raise ToolExecutionError(f"{self.name}: unknown argument(s): {', '.join(unknown)}", tool=self.name)
        missing = [key for key in required if key not in arguments]
        if missing: raise ToolExecutionError(f"{self.name}: missing required argument(s): {', '.join(missing)}", tool=self.name)
        return arguments

    def __repr__(self) -> str: return f"<Tool {self.name} risk={self.risk.name}>"


class FunctionTool(Tool):
    def __init__(self, name: str, description: str = "", risk: RiskLevel | str = RiskLevel.SAFE,
                 handler: Callable[..., Any] | None = None, *, parameters: dict[str, Any] | None = None,
                 required_permissions: set[str] | frozenset[str] | None = None, network: bool = False,
                 tags: set[str] | frozenset[str] | None = None) -> None:
        if handler is None: raise ValueError(f"FunctionTool {name!r} requires a handler")
        self.name, self.description = name, description
        self.risk = RiskLevel.parse(risk, RiskLevel.MEDIUM)
        self.handler, self.parameters = handler, parameters or _infer_parameters(handler)
        self.required_permissions = frozenset(required_permissions or ())
        self.network, self.tags = network, frozenset(tags or ())
        self._wants_context = "context" in inspect.signature(handler).parameters

    async def run(self, context: ToolContext, **arguments: Any) -> Any:
        if self._wants_context: arguments = {**arguments, "context": context}
        result = self.handler(**arguments)
        return await result if inspect.isawaitable(result) else result

    @property
    def _handler(self) -> Callable[..., Any]: return self.handler


def _infer_parameters(handler: Callable[..., Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}; required: list[str] = []
    try: signature = inspect.signature(handler)
    except (TypeError, ValueError): return {"type": "object", "properties": {}, "required": []}
    for name, parameter in signature.parameters.items():
        if name in {"self", "context"} or parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}: continue
        properties[name] = {"type": _json_type(parameter.annotation)}
        if parameter.default is inspect.Parameter.empty: required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def _json_type(annotation: Any) -> str:
    return {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}.get(annotation, "string")


__all__ = ["FunctionTool", "RiskLevel", "Tool", "ToolContext", "ToolResult", "provider_safe_name"]
