from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time
import uuid


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "default"
    messages: list[dict[str, str]] = field(default_factory=list)
    updated: float = field(default_factory=time.time)


class SessionManager:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".airvis" / "sessions.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, Session] = {}
        self._load()

    def get(self, name: str = "default") -> Session:
        if name not in self.sessions:
            self.sessions[name] = Session(name=name)
        session = self.sessions[name]
        session.updated = time.time()
        self._save()
        return session

    def list(self) -> list[dict[str, object]]:
        return [{"id": s.id, "name": s.name, "messages": len(s.messages), "updated": s.updated} for s in self.sessions.values()]

    def _save(self) -> None:
        data = {name: {"id": session.id, "name": session.name, "messages": session.messages, "updated": session.updated} for name, session in self.sessions.items()}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for name, item in data.items():
                self.sessions[name] = Session(item["id"], item["name"], item.get("messages", []), item.get("updated", time.time()))
        except (OSError, ValueError, KeyError, TypeError):
            self.sessions = {}
