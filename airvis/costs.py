from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import time


@dataclass
class CostTracker:
    path: Path | None = None
    total: float = 0.0

    def __post_init__(self) -> None:
        self.path = self.path or Path.home() / ".airvis" / "costs.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            try:
                self.total = float(json.loads(self.path.read_text()).get("total", 0))
            except (OSError, ValueError, TypeError):
                self.total = 0.0

    def record(self, provider: str, model: str, input_tokens: int = 0, output_tokens: int = 0, rate_per_million: float = 0.0) -> float:
        amount = (input_tokens + output_tokens) / 1_000_000 * rate_per_million
        self.total += amount
        self.path.write_text(json.dumps({"total": self.total, "last": {"time": time.time(), "provider": provider, "model": model, "amount": amount}}, indent=2), encoding="utf-8")
        return amount
