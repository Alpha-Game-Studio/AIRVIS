"""AIRVIS Agent OS: persistent sessions, sub-agents, background jobs and event streaming."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .core.asyncutil import run_blocking
from .engine import AirvisEngine
from .sessions import SessionManager
from .state.store import MemoryStore


@dataclass
class BackgroundJob:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    request: str = ""
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    workflow_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class AgentOS:
    """Long-lived autonomous runtime around the native AIRVIS engine."""

    def __init__(self, engine: AirvisEngine, *, root: Path | str | None = None, max_workers: int = 4) -> None:
        self.engine = engine
        base = Path(root or engine.workspace or Path.cwd()).expanduser()
        self.root = base
        self.sessions = SessionManager(base / ".airvis" / "sessions.json")
        self.memory = engine.memory if hasattr(engine, "memory") else MemoryStore(base / ".airvis" / "memory.db")
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="airvis-agent")
        self._jobs: dict[str, BackgroundJob] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._children: dict[str, list[str]] = {}
        self._lock = threading.RLock()

    def session(self, name: str = "default") -> dict[str, Any]:
        return self.sessions.get(name).__dict__.copy()

    def sessions_list(self) -> list[dict[str, Any]]:
        return self.sessions.list()

    def reset_session(self, name: str = "default") -> bool:
        session = self.sessions.sessions.get(name)
        if session is None:
            return False
        session.messages.clear()
        session.updated = time.time()
        self.sessions._save()
        return True

    def remember(self, content: str) -> str:
        return self.memory.add(content.strip())

    def recall(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        memories = self.memory.list()
        terms = [item.lower() for item in query.split() if item.strip()]
        if not terms:
            return memories[:limit]
        ranked: list[tuple[int, dict[str, Any]]] = []
        for item in memories:
            text = str(item.get("content", "")).lower()
            score = sum(text.count(term) for term in terms)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1].get("created", 0)), reverse=True)
        return [item for _, item in ranked[:limit]]

    def build_context(self, prompt: str, session_name: str = "default") -> dict[str, Any]:
        session = self.sessions.get(session_name)
        return {"session": session_name, "messages": session.messages[-24:], "memories": self.recall(prompt)}

    def spawn(self, request: str, *, parent_job_id: str | None = None, strategy: str | None = None) -> str:
        """Start an independent native AIRVIS workflow in the background."""
        job = BackgroundJob(request=request.strip())
        with self._lock:
            self._jobs[job.id] = job
            if parent_job_id:
                self._children.setdefault(parent_job_id, []).append(job.id)
            self._futures[job.id] = self._executor.submit(self._run_job, job.id, strategy)
        return job.id

    def _run_job(self, job_id: str, strategy: str | None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.started_at = time.time()
        try:
            result = run_blocking(self.engine.run(job.request, strategy=strategy))
            with self._lock:
                job.status = "completed" if result.ok else "failed"
                job.workflow_id = result.workflow_id
                job.result = result.to_dict()
                job.finished_at = time.time()
        except Exception as exc:
            with self._lock:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = time.time()

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.to_dict() for job in self._jobs.values()]

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.workflow_id:
                return self.engine.cancel(job.workflow_id)
            future = self._futures.get(job_id)
            return bool(future and future.cancel())

    def children(self, job_id: str) -> list[dict[str, Any]]:
        return [item for child in self._children.get(job_id, []) if (item := self.job(child))]

    def run_goal(self, goal: str, *, max_steps: int = 8, strategy: str | None = None) -> str:
        """Run a bounded autonomous goal through the native workflow engine."""
        del max_steps  # each AIRVIS workflow already has its own repair/iteration budget
        job_id = self.spawn(goal, strategy=strategy)
        deadline = time.time() + 3600
        while time.time() < deadline:
            current = self.job(job_id)
            if not current:
                return "goal job disappeared"
            if current["status"] in {"completed", "failed"}:
                return str((current.get("result") or {}).get("output") or current.get("error") or "")
            time.sleep(0.1)
        self.cancel_job(job_id)
        return "goal timed out"

    async def events(self, workflow_id: str | None = None, *, poll_seconds: float = 0.25) -> AsyncIterator[dict[str, Any]]:
        """Stream durable workflow events without requiring a separate broker."""
        seen: set[str] = set()
        while True:
            records = self.engine.store.list_events(workflow_id, limit=500)
            emitted = False
            for event in records:
                event_id = str(event.get("id", ""))
                if event_id and event_id not in seen:
                    seen.add(event_id)
                    emitted = True
                    yield event
            if workflow_id:
                state = self.engine.store.load_workflow(workflow_id)
                if state and state.get("status") in {"completed", "failed", "cancelled"} and not emitted:
                    return
            await asyncio.sleep(poll_seconds)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["AgentOS", "BackgroundJob"]
