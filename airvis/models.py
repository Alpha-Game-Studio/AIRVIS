from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    name: str
    provider: str
    capabilities: frozenset[str]
    local: bool = False
    cost_per_million_tokens: float = 0.0


class ModelCatalog:
    def __init__(self) -> None:
        self.models = [
            Model("llama3.2", "ollama", frozenset({"chat", "tools"}), True),
            Model("gpt-4o-mini", "openai", frozenset({"chat", "tools", "vision"}), False, 0.15),
            Model("claude-3-5-sonnet-latest", "anthropic", frozenset({"chat", "tools", "vision"}), False, 3.0),
            Model("gemini-2.0-flash", "gemini", frozenset({"chat", "tools", "vision"}), False, 0.1),
            Model("grok-2-latest", "xai", frozenset({"chat", "tools"}), False),
        ]

    def list(self, capability: str | None = None, local: bool | None = None) -> list[dict[str, object]]:
        result = [model for model in self.models if not capability or capability in model.capabilities]
        if local is not None:
            result = [model for model in result if model.local == local]
        return [{"name": model.name, "provider": model.provider, "capabilities": sorted(model.capabilities), "local": model.local, "cost_per_million_tokens": model.cost_per_million_tokens} for model in result]
