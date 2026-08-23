from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CostTracker:
    path: Path | None = None
    total: float = 0.0

    def __post_init__(self) -> None:
        self.path = self.path or Path.home() / ".airvis" / "costs.json"
        target = self.resolved_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            try:
                self.total = float(json.loads(target.read_text()).get("total", 0))
            except (OSError, ValueError, TypeError):
                self.total = 0.0

    @property
    def resolved_path(self) -> Path:
        """``path`` is always populated by ``__post_init__``."""
        assert self.path is not None
        return self.path

    def record(self, provider: str, model: str, input_tokens: int = 0, output_tokens: int = 0, rate_per_million: float = 0.0) -> float:
        amount = (input_tokens + output_tokens) / 1_000_000 * rate_per_million
        self.total += amount
        self.resolved_path.write_text(json.dumps({"total": self.total, "last": {"time": time.time(), "provider": provider, "model": model, "amount": amount}}, indent=2), encoding="utf-8")
        return amount
