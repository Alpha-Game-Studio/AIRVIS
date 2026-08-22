from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass
class RoutingPolicy:
    mode: str = "AUTO"
    privacy: str = "AUTO"
    daily_budget: float = 0.0


class ModelRouter:
    def __init__(self, provider_id: str = "mock", model: str = "", policy: RoutingPolicy | None = None) -> None:
        self.provider_id = provider_id
        self.model = model
        self.policy = policy or RoutingPolicy(
            mode=os.environ.get("AIRVIS_ROUTING_MODE", "AUTO").upper(),
            privacy=os.environ.get("AIRVIS_PRIVACY_MODE", "AUTO").upper(),
            daily_budget=float(os.environ.get("AIRVIS_DAILY_BUDGET", "0") or 0),
        )

    def choose(self, task: str, requires_network: bool = False) -> dict[str, str | bool]:
        local_only = self.policy.privacy == "LOCAL ONLY" or self.policy.privacy == "LOCAL"
        if local_only:
            return {"provider": "ollama", "model": os.environ.get("OLLAMA_MODEL", "llama3.2"), "local": True}
        if self.policy.mode == "MANUAL":
            return {"provider": self.provider_id, "model": self.model, "local": self.provider_id in {"ollama", "mock"}}
        if requires_network:
            return {"provider": self.provider_id, "model": self.model, "local": False}
        return {"provider": "ollama" if os.environ.get("OLLAMA_HOST") else self.provider_id, "model": self.model, "local": bool(os.environ.get("OLLAMA_HOST"))}

    def status(self) -> dict[str, object]:
        return {"mode": self.policy.mode, "privacy": self.policy.privacy, "daily_budget": self.policy.daily_budget, "selected": self.choose("general")}
