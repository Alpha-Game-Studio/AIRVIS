"""SQLite persistence for workflows, tasks, events, artifacts and repairs.

Everything is stored as JSON payloads keyed by id so the schema does not have to
track every dataclass field; the store's job is durability and recovery.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    workflow_id TEXT,
    status TEXT NOT NULL,
    updated_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    workflow_id TEXT,
    task_id TEXT,
    type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    workflow_id TEXT,
    task_id TEXT,
    type TEXT,
    created_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    workflow_id TEXT,
    task_id TEXT,
    status TEXT,
    created_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repairs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT,
    task_id TEXT,
    strategy TEXT,
    created_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks(workflow_id);
CREATE INDEX IF NOT EXISTS idx_events_workflow ON events(workflow_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_workflow ON artifacts(workflow_id);
"""


class StateStore:
    """Durable execution state; an interrupted workflow can be resumed from it."""

    def __init__(self, path: Path | str | None = None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.path = Path(path).expanduser() if path else Path.home() / ".airvis" / "state.db"
        self._lock = threading.RLock()
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    # -- workflows -------------------------------------------------------------

    def save_workflow(self, workflow: dict[str, Any]) -> None:
        if not self.enabled:
            return
        workflow_id = str(workflow.get("workflow_id") or workflow.get("id") or uuid.uuid4().hex)
        now = time.time()
        with self._lock, self._connect() as db:
            existing = db.execute("SELECT created_at FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
            created = float(existing["created_at"]) if existing else now
            db.execute(
                "INSERT OR REPLACE INTO workflows (id, request, status, created_at, updated_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (workflow_id, str(workflow.get("request", "")), str(workflow.get("status", "created")),
                 created, now, _dumps(workflow)),
            )

    def load_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_workflows(self, limit: int = 50, *, status: str | None = None) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        query = "SELECT id, request, status, created_at, updated_at FROM workflows"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, limit))
        with self._lock, self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def delete_workflow(self, workflow_id: str) -> None:
        if not self.enabled:
            return
        with self._lock, self._connect() as db:
            for table in ("workflows", "tasks", "events", "artifacts", "reviews", "repairs"):
                column = "id" if table == "workflows" else "workflow_id"
                db.execute(f"DELETE FROM {table} WHERE {column} = ?", (workflow_id,))

    # -- tasks -----------------------------------------------------------------

    def save_task(self, task: dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO tasks (id, workflow_id, status, updated_at, payload) VALUES (?, ?, ?, ?, ?)",
                (str(task.get("id")), task.get("workflow_id"), str(task.get("status", "queued")),
                 time.time(), _dumps(task)),
            )

    def load_tasks(self, workflow_id: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT payload FROM tasks WHERE workflow_id = ? ORDER BY updated_at", (workflow_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def load_task(self, task_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    # -- events / artifacts / reviews / repairs ---------------------------------

    def save_event(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO events (id, workflow_id, task_id, type, timestamp, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (str(event.get("id") or uuid.uuid4().hex), event.get("workflow_id"), event.get("task_id"),
                 str(event.get("type", "")), float(event.get("timestamp", time.time())), _dumps(event)),
            )

    def record_event(self, event: Any) -> None:
        """Persist an EventBus event in the canonical event table.

        EventBus emits Event objects, while the state store historically exposed
        ``save_event`` for dictionaries. Keep both APIs and normalize at this
        boundary so persistence never depends on the caller's event representation.
        """
        if not self.enabled:
            return
        if isinstance(event, dict):
            payload = dict(event)
        elif hasattr(event, "to_dict"):
            payload = dict(event.to_dict())
        else:
            payload = {
                key: getattr(event, key)
                for key in ("id", "workflow_id", "task_id", "type", "timestamp")
                if hasattr(event, key)
            }
            if hasattr(event, "data") and isinstance(event.data, dict):
                payload.update(event.data)
        self.save_event(payload)

    def list_events(self, workflow_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        query = "SELECT payload FROM events"
        params: list[Any] = []
        if workflow_id:
            query += " WHERE workflow_id = ?"
            params.append(workflow_id)
        query += " ORDER BY timestamp LIMIT ?"
        params.append(max(1, limit))
        with self._lock, self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_artifact(self, artifact: dict[str, Any]) -> None:
        self._save_generic("artifacts", artifact, {"type": artifact.get("type")})

    def list_artifacts(self, workflow_id: str) -> list[dict[str, Any]]:
        return self._list_generic("artifacts", workflow_id)

    def save_review(self, workflow_id: str | None, review: dict[str, Any]) -> None:
        payload = {**review, "workflow_id": workflow_id}
        self._save_generic("reviews", payload, {"status": review.get("status")})

    def list_reviews(self, workflow_id: str) -> list[dict[str, Any]]:
        return self._list_generic("reviews", workflow_id)

    def save_repair(self, workflow_id: str | None, task_id: str | None, repair: dict[str, Any]) -> None:
        payload = {**repair, "workflow_id": workflow_id, "task_id": task_id}
        self._save_generic("repairs", payload, {"strategy": repair.get("strategy")})

    def list_repairs(self, workflow_id: str) -> list[dict[str, Any]]:
        return self._list_generic("repairs", workflow_id)

    def _save_generic(self, table: str, payload: dict[str, Any], extra: dict[str, Any]) -> None:
        if not self.enabled:
            return
        column, value = next(iter(extra.items()))
        with self._lock, self._connect() as db:
            db.execute(
                f"INSERT OR REPLACE INTO {table} (id, workflow_id, task_id, {column}, created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (str(payload.get("id") or uuid.uuid4().hex), payload.get("workflow_id"), payload.get("task_id"),
                 str(value) if value is not None else None, time.time(), _dumps(payload)),
            )

    def _list_generic(self, table: str, workflow_id: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock, self._connect() as db:
            rows = db.execute(f"SELECT payload FROM {table} WHERE workflow_id = ? ORDER BY created_at", (workflow_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]


class MemoryStore:
    """Long-term memory."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser() if path else Path.home() / ".airvis" / "memory.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, created REAL NOT NULL)")

    def add(self, content: str) -> str:
        memory_id = uuid.uuid4().hex
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO memories VALUES (?, ?, ?)", (memory_id, content, time.time()))
        return memory_id

    def list(self) -> builtins.list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT id, content, created FROM memories ORDER BY created DESC").fetchall()
        return [{"id": row[0], "content": row[1], "created": row[2]} for row in rows]

    def delete(self, memory_id: str) -> bool:
        with sqlite3.connect(self.path) as db:
            cursor = db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = ["MemoryStore", "StateStore"]
