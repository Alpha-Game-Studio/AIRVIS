"""Deprecated module kept for backward compatibility.

The canonical provider registry — with health tracking, capability detection and
an explicit fallback chain — is :class:`airvis.providers.ProviderRegistry`.
``ProviderManager`` remains only as a thin failover helper for V4 duck-typed
providers that expose ``chat(messages, tools) -> str``.
"""

from __future__ import annotations

import builtins
from typing import Any, Protocol

from .providers.registry import ProviderRegistry

__all__ = ["Provider", "ProviderManager", "ProviderRegistry"]


class Provider(Protocol):
    id: str
    capabilities: set[str]

    def chat(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> str: ...


class ProviderManager:
    """Sequential failover over V4-style providers."""

    def __init__(self, providers: builtins.list[Provider] | None = None) -> None:
        self.providers = list(providers or [])

    def add(self, provider: Provider) -> None:
        self.providers.append(provider)

    def list(self) -> builtins.list[dict[str, Any]]:
        return [
            {"id": provider.id, "capabilities": sorted(provider.capabilities), "status": "ready"}
            for provider in self.providers
        ]

    def chat(self, messages: builtins.list[dict[str, str]], tools: builtins.list[dict[str, Any]]) -> str:
        if not self.providers:
            raise RuntimeError("No provider configured")
        failures: builtins.list[str] = []
        for provider in self.providers:
            try:
                return provider.chat(messages, tools)
            except Exception as exc:
                failures.append(f"{provider.id}: {exc}")
        raise RuntimeError("All providers failed: " + "; ".join(failures))
