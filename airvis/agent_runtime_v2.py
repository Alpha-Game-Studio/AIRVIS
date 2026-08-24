"""Autonomous agent runtime: model-directed tool loop with bounded delegation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .tool_runtime import ToolRuntime


@dataclass
class RuntimeStep:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


class AutonomousAgent:
    def __init__(self, name: str, model: Callable[[list[dict[str, Any]], list[dict[str, Any]]], Awaitable[dict[str, Any]]], tools: ToolRuntime, *, max_steps: int = 30) -> None:
        self.name = name
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        self.steps: list[RuntimeStep] = []

    async def run(self, goal: str, *, context: list[dict[str, Any]] | None = None, approved_tools: set[str] | None = None) -> dict[str, Any]:
        messages = list(context or [])
        messages.append({"role": "user", "content": goal})
        tool_descriptions = self.tools.describe()
        for _ in range(self.max_steps):
            decision = await self.model(messages, tool_descriptions)
            self.steps.append(RuntimeStep("model", decision))
            if decision.get("final") is not None:
                return {"status": "completed", "result": decision["final"], "steps": [s.__dict__ for s in self.steps]}
            tool = decision.get("tool")
            if not tool:
                messages.append({"role": "system", "content": "You must either call a tool or provide final output."})
                continue
            result = await self.tools.execute(tool, decision.get("arguments", {}), approved=tool in (approved_tools or set()))
            self.steps.append(RuntimeStep("tool", {"tool": tool, "result": result.__dict__}))
            messages.append({"role": "tool", "name": tool, "content": result.output if result.ok else f"ERROR: {result.error}"})
        return {"status": "exhausted", "result": None, "steps": [s.__dict__ for s in self.steps]}


__all__ = ["AutonomousAgent", "RuntimeStep"]
