from __future__ import annotations

from dataclasses import dataclass, field
import uuid


@dataclass
class SubAgent:
    role: str
    tools: set[str] = field(default_factory=set)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class AgentDelegator:
    def __init__(self) -> None:
        self.agents = {
            "research": SubAgent("research", {"web", "filesystem.search"}),
            "coding": SubAgent("coding", {"filesystem.read", "filesystem.search", "terminal.execute"}),
            "test": SubAgent("test", {"terminal.execute", "filesystem.read"}),
        }

    def list(self) -> list[dict[str, object]]:
        return [{"id": agent.id, "role": agent.role, "tools": sorted(agent.tools)} for agent in self.agents.values()]

    def delegate(self, role: str, prompt: str, runtime) -> str:
        if role not in self.agents:
            raise KeyError(f"Unknown agent role: {role}")
        return runtime.run(f"[{role} agent] {prompt}")
