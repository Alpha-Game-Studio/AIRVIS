"""High-level autonomous agent: provider-native tool calls plus delegation."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any
from .providers.base import GenerationRequest, Message
from .tool_runtime import ToolRuntime

@dataclass
class AutonomousResult:
    status: str
    text: str = ""
    steps: int = 0
    tool_calls: int = 0
    delegated: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

class AutonomousLoop:
    """Real provider-native tool loop; no one-shot generation wrapper."""
    def __init__(self, provider: Any, tools: ToolRuntime, *, model: str = "", max_steps: int = 40):
        self.provider, self.tools, self.model, self.max_steps = provider, tools, model, max_steps

    async def run(self, goal: str, *, system: str | None = None, approved_tools: set[str] | None = None) -> AutonomousResult:
        messages = [Message("system", system)] if system else []
        messages.append(Message("user", goal))
        history, delegated = [], []
        schemas = [self._schema(item) for item in self.tools.describe()]
        for step in range(1, self.max_steps + 1):
            response = await self.provider.generate(GenerationRequest.build(messages, model=self.model, tools=schemas, max_tokens=4096))
            history.append({"step": step, "text": response.text, "tool_calls": [c.to_dict() for c in response.tool_calls]})
            if not response.tool_calls:
                return AutonomousResult("completed", response.text, step, len(history), delegated, history)
            messages.append(Message("assistant", response.text))
            for call in response.tool_calls:
                if call.name == "agent.delegate":
                    prompt = str(call.arguments.get("goal", "")).strip()
                    if prompt: delegated.append(prompt)
                    result = {"ok": True, "delegated": prompt}
                else:
                    result = (await self.tools.execute(call.name, call.arguments, approved=call.name in (approved_tools or set()))).__dict__
                messages.append(Message("tool", json.dumps(result, ensure_ascii=False), name=call.name, tool_call_id=call.id))
        return AutonomousResult("exhausted", "Agent step budget exhausted.", self.max_steps, len(history), delegated, history)

    @staticmethod
    def _schema(item: dict[str, Any]) -> dict[str, Any]:
        name = item["name"]
        if name == "agent.delegate":
            params = {"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]}
        elif name == "filesystem.read":
            params = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        elif name == "filesystem.write":
            params = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
        elif name == "filesystem.list":
            params = {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}
        else:
            params = {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number", "default": 60}}, "required": ["command"]}
        return {"name": name, "description": item["description"], "parameters": params}

__all__ = ["AutonomousLoop", "AutonomousResult"]
