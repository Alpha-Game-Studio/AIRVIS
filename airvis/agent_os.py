"""AIRVIS Agent OS: persistent runtime built around the Agent Kernel."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .agent_kernel import AgentKernel, AgentGoal, AgentTask, KernelEvent, KernelPolicy
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
    """Long-lived AIRVIS runtime with autonomous goal supervision."""

    def __init__(self, engine: AirvisEngine, *, root: Path | str | None = None, max_workers: int = 4) -> None:
        self.engine = engine
        self.root = Path(root or engine.workspace or Path.cwd()).expanduser().resolve()
        self.sessions = SessionManager(self.root / ".airvis" / "sessions.json")
        self.memory = engine.memory if hasattr(engine, "memory") else MemoryStore(self.root / ".airvis" / "memory.db")
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="airvis-agent")
        self._jobs: dict[str, BackgroundJob] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._children: dict[str, list[str]] = {}
        self._lock = threading.RLock()
        self.kernel = AgentKernel(
            self._execute_goal_task,
            policy=KernelPolicy(max_parallel_agents=max_workers),
            event_sink=self._kernel_event,
        )

    def _kernel_event(self, event: KernelEvent) -> None:
        try:
            self.engine.event_bus.emit(event.type, {"goal_id": event.goal_id, "task_id": event.task_id, **event.data})
        except Exception:
            pass

    async def _execute_goal_task(self, request: str, *, strategy: str | None = None, agent: str = "general", **_: Any) -> Any:
        result = await self.engine.run(request, strategy=strategy)
        return result.to_dict()

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
        ranked = []
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

    async def run_autonomous(self, prompt: str, *, strategy: str | None = None, max_iterations: int = 20) -> AgentGoal:
        return await self.kernel.run(prompt, strategy=strategy, max_iterations=max_iterations)

    def spawn(self, request: str, *, parent_job_id: str | None = None, strategy: str | None = None) -> str:
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
            goal = run_blocking(self.run_autonomous(job.request, strategy=strategy))
            with self._lock:
                job.status = "completed" if goal.status == "completed" else "failed"
                job.result = {"goal_id": goal.id, "status": goal.status, "iterations": goal.iteration, "result": goal.result, "history": goal.history}
                job.finished_at = time.time()
        except Exception as exc:
            with self._lock:
                job.status, job.error = "failed", str(exc)
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
            future = self._futures.get(job_id)
            return bool(future and future.cancel())

    def children(self, job_id: str) -> list[dict[str, Any]]:
        return [item for child in self._children.get(job_id, []) if (item := self.job(child))]

    def run_goal(self, goal: str, *, max_steps: int = 20, strategy: str | None = None) -> str:
        result = run_blocking(self.run_autonomous(goal, strategy=strategy, max_iterations=max_steps))
        return str(result.result or result.status)

    async def events(self, workflow_id: str | None = None, *, poll_seconds: float = 0.25) -> AsyncIterator[dict[str, Any]]:
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
