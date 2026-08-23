"""Public extension API for plugins and integrations."""

from __future__ import annotations

from .agents.spec import AgentSpec
from .backends.base import Backend, ExecutionRequest, ExecutionResult
from .plugins import Plugin
from .providers.base import GenerationRequest, GenerationResult, Message, Provider, ProviderCapabilities
from .tools.base import FunctionTool, RiskLevel, ToolContext, ToolResult
from .tools.base import Tool as ToolBase

#: V4 alias: ``Tool("name", "description", "RISK", handler)``.
Tool = FunctionTool

__all__ = [
    "AgentSpec",
    "Backend",
    "ExecutionRequest",
    "ExecutionResult",
    "FunctionTool",
    "GenerationRequest",
    "GenerationResult",
    "Message",
    "Plugin",
    "Provider",
    "ProviderCapabilities",
    "RiskLevel",
    "Tool",
    "ToolBase",
    "ToolContext",
    "ToolResult",
]
