from __future__ import annotations

from typing import Any, Protocol


class Provider(Protocol):
    id: str
    capabilities: set[str]

    def chat(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> str: ...


class ProviderManager:
    def __init__(self, providers: list[Provider] | None = None) -> None:
        self.providers = providers or []

    def add(self, provider: Provider) -> None:
        self.providers.append(provider)

    def list(self) -> list[dict[str, Any]]:
        return [{"id": provider.id, "capabilities": sorted(provider.capabilities), "status": "ready"} for provider in self.providers]

    def chat(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> str:
        if not self.providers:
            raise RuntimeError("No provider configured")
        failures: list[str] = []
        for provider in self.providers:
            try:
                return provider.chat(messages, tools)
            except Exception as exc:
                failures.append(f"{provider.id}: {exc}")
        raise RuntimeError("All providers failed: " + "; ".join(failures))
