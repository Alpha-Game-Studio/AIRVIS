from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_SECRET = re.compile(r"(?i)(api[_ -]?key|token|password|secret)\s*[=:]\s*[^\s,;]+")
_SECRET_FIELD = re.compile(r"(?i)(api[_ -]?key|token|password|secret)")


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub(r"\1=[REDACTED]", value)
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _SECRET_FIELD.search(str(key)) else sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".airvis" / "logs" / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, action: str, **details: Any) -> None:
        entry = {"time": time.time(), "action": action, **sanitize(details)}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
