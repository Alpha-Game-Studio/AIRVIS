"""Model catalogue.

When a :class:`~airvis.providers.registry.ProviderRegistry` is supplied the
catalogue reflects the providers that are actually registered; otherwise it
falls back to the static V4 table.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Model:
    name: str
    provider: str
    capabilities: frozenset[str]
    local: bool = False
    cost_per_million_tokens: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "capabilities": sorted(self.capabilities),
            "local": self.local,
            "cost_per_million_tokens": self.cost_per_million_tokens,
        }


STATIC_MODELS: tuple[Model, ...] = (
    Model("llama3.2", "ollama", frozenset({"chat", "tools"}), True),
    Model("gpt-4o-mini", "openai", frozenset({"chat", "tools", "vision"}), False, 0.15),
    Model("claude-sonnet-5", "anthropic", frozenset({"chat", "tools", "vision"}), False, 3.0),
    Model("gemini-2.0-flash", "gemini", frozenset({"chat", "tools", "vision"}), False, 0.1),
    Model("grok-2-latest", "xai", frozenset({"chat", "tools"}), False, 2.0),
)


class ModelCatalog:
    def __init__(self, providers: Any = None) -> None:
        self.providers = providers
        self.models: builtins.list[Model] = list(STATIC_MODELS)

    def _current(self) -> builtins.list[Model]:
        if self.providers is None:
            return list(self.models)
        derived: builtins.list[Model] = []
        for provider in self.providers:
            names = provider.models or ((provider.default_model,) if provider.default_model else ())
            for name in names:
                derived.append(
                    Model(
                        name=name,
                        provider=provider.id,
                        capabilities=frozenset(provider.capabilities.names()),
                        local=provider.local,
                        cost_per_million_tokens=(
                            provider.cost_per_million_input + provider.cost_per_million_output
                        )
                        / 2.0,
                    )
                )
        return derived or list(self.models)

    def list(self, capability: str | None = None, local: bool | None = None) -> builtins.list[dict[str, Any]]:
        models = self._current()
        if capability:
            models = [model for model in models if capability in model.capabilities]
        if local is not None:
            models = [model for model in models if model.local == local]
        return [model.to_dict() for model in models]


__all__ = ["STATIC_MODELS", "Model", "ModelCatalog"]
