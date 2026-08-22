from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TaskStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".airvis" / "tasks.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, tasks: dict[str, dict[str, Any]]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
