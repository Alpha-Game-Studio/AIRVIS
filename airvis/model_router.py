"""Deprecated module kept for backward compatibility.

Routing is now performed by :class:`airvis.agents.router.AgentRouter` using the
strategy and weights in :class:`airvis.core.config.RoutingConfig`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .compat import LegacyModelRouter

__all__ = ["LegacyModelRouter", "ModelRouter", "RoutingPolicy"]


@dataclass
class RoutingPolicy:
    mode: str = "AUTO"
    privacy: str = "AUTO"
    daily_budget: float = 0.0


class ModelRouter:
    """V4 provider/model chooser retained for older integrations."""

    def __init__(self, provider_id: str = "mock", model: str = "", policy: RoutingPolicy | None = None) -> None:
        self.provider_id = provider_id
        self.model = model
        self.policy = policy or RoutingPolicy(
            mode=os.environ.get("AIRVIS_ROUTING_MODE", "AUTO").upper(),
            privacy=os.environ.get("AIRVIS_PRIVACY_MODE", "AUTO").upper(),
            daily_budget=float(os.environ.get("AIRVIS_DAILY_BUDGET", "0") or 0),
        )

    def choose(self, task: str, requires_network: bool = False) -> dict[str, str | bool]:
        if self.policy.privacy in {"LOCAL ONLY", "LOCAL", "LOCAL_ONLY"}:
            return {"provider": "ollama", "model": os.environ.get("OLLAMA_MODEL", "llama3.2"), "local": True}
        if self.policy.mode == "MANUAL":
            return {
                "provider": self.provider_id,
                "model": self.model,
                "local": self.provider_id in {"ollama", "mock"},
            }
        if requires_network:
            return {"provider": self.provider_id, "model": self.model, "local": False}
        host = os.environ.get("OLLAMA_HOST")
        return {
            "provider": "ollama" if host else self.provider_id,
            "model": self.model,
            "local": bool(host),
        }

    def status(self) -> dict[str, object]:
        return {
            "mode": self.policy.mode,
            "privacy": self.policy.privacy,
            "daily_budget": self.policy.daily_budget,
            "selected": self.choose("general"),
        }
