"""AIRVIS V4-compatible runtime facade over the V6 orchestration engine.

``AgentRuntime`` keeps the synchronous surface the desktop assistant, the web
server and the CLI were written against, but every call now travels through the
real pipeline: planner -> DAG -> agent router -> backend -> provider -> tools ->
review -> repair.

New code should use :class:`airvis.engine.AirvisEngine` directly.
"""

from __future__ import annotations

import re
import threading
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .compat import LegacyAgentDelegator, LegacyModelRouter, LegacyProviderAdapter, LegacyProviderManager
from .core.asyncutil import run_blocking
from .core.config import AirvisConfig
from .core.errors import ApprovalRequiredError, PermissionDeniedError
from .costs import CostTracker
from .engine import AirvisEngine
from .models import ModelCatalog
from .orchestration.task import Task, TaskStatus, WorkflowStatus
from .plugins import PluginManager
from .providers.base import Provider
from .providers.factory import provider_from_environment
from .providers.http import AnthropicProvider, GeminiProvider, OpenAICompatibleProvider
from .providers.mock import MockProvider
from .scheduler import Scheduler
from .security.permissions import always_approve
from .sessions import SessionManager
from .state.store import MemoryStore
from .task_store import TaskStore
from .tools.base import FunctionTool, RiskLevel
from .tools.registry import ToolRegistry, command_risk

#: V4 name kept as an alias so ``except PermissionError`` in old callers still works.
PermissionError = PermissionDeniedError

#: V4 alias: tools used to be a dataclass of (name, description, risk, handler).
Tool = FunctionTool


class AgentState(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    EXECUTING = "executing"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_MEMORY_REQUEST = re.compile(r"(?:이거|이 내용을)\s*기억해[:：]?\s*(.*)", re.IGNORECASE)


class AgentRuntime:
    """Synchronous facade over :class:`~airvis.engine.AirvisEngine`."""

    def __init__(
        self,
        workspace: Path | str | None = None,
        provider: Provider | Any | None = None,
        max_iterations: int = 4,
        memory_path: Path | None = None,
        task_path: Path | None = None,
        audit_path: Path | None = None,
        session_path: Path | None = None,
        timeout_seconds: float = 120.0,
        max_tool_calls: int = 20,
        *,
        config: AirvisConfig | None = None,
        approval_handler: Any = None,
    ) -> None:
        root = Path(workspace or Path.cwd()).resolve()
        settings = config or AirvisConfig.load()
        settings.workspace = str(root)
        settings.backends.max_tool_calls = max_tool_calls
        settings.workflow.task_timeout = max(settings.workflow.task_timeout, timeout_seconds)

        # Keep every store beside the caller's paths so tests stay self-contained.
        sidecar = Path(memory_path).parent if memory_path else None
        self.engine = AirvisEngine(
            settings,
            workspace=root,
            approval_handler=approval_handler,
            state_path=(sidecar / "state.db") if sidecar else None,
            memory_path=memory_path,
            artifact_root=(sidecar / "artifacts") if sidecar else None,
        )
        for agent in self.engine.agents.all():
            agent.max_iterations = max(1, max_iterations)

        if provider is not None:
            wrapped = provider if isinstance(provider, Provider) else LegacyProviderAdapter(provider)
            self.engine.providers.register(wrapped)
            self.engine.providers.fallbacks = [wrapped.id, *self.engine.providers.fallbacks]
            for agent in self.engine.agents.all():
                agent.provider_id = wrapped.id
                agent.model = wrapped.default_model or None

        self.state = AgentState.IDLE
        self.session_id = uuid.uuid4().hex
        self.max_iterations = max(1, max_iterations)
        self.timeout_seconds = max(1.0, timeout_seconds)
        self.max_tool_calls = max(1, max_tool_calls)
        self.tool_calls = 0
        self.cancel_event = threading.Event()
        self.last_error: str | None = None
        self.messages: list[dict[str, str]] = []
        self._workflow_id: str | None = None

        # -- V4 attribute surface ---------------------------------------------
        self.provider: Provider = self.engine.providers.default
        self.provider_manager = LegacyProviderManager(self.engine.providers)
        self.tools: ToolRegistry = self.engine.tools
        self.permissions = self.engine.permissions
        self.memory: MemoryStore = self.engine.memory
        self.artifacts = self.engine.artifacts
        self.sessions = SessionManager(session_path)
        self.planner = self.engine.planner
        self.scheduler = Scheduler()
        self.agents = LegacyAgentDelegator(self.engine)
        self.plugins = PluginManager()
        self.task_store = TaskStore(task_path)
        self.audit = AuditLog(audit_path)
        self.router = LegacyModelRouter(self.engine.config, self.engine.providers)
        self.catalog = ModelCatalog(self.engine.providers)
        self.costs = CostTracker()
        self.tasks: dict[str, Task] = {}
        self._restore_tasks()

    # -- providers -------------------------------------------------------------

    def providers(self) -> list[dict[str, Any]]:
        return self.engine.providers.list()

    # -- execution -------------------------------------------------------------

    def run(self, prompt: str) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            return "명령을 입력해주세요."

        self.state = AgentState.THINKING
        self.cancel_event.clear()
        self.last_error = None
        self.audit.record("agent.run", session=self.session_id, prompt=prompt)

        session = self.sessions.get()
        session.messages.append({"role": "user", "content": prompt})
        self.messages = session.messages

        memory_request = _MEMORY_REQUEST.match(prompt)
        if memory_request and memory_request.group(1).strip():
            self.memory.add(memory_request.group(1).strip())
            answer = "기억해두었습니다."
            session.messages.append({"role": "assistant", "content": answer})
            self.sessions._save()
            self.state = AgentState.COMPLETED
            return answer

        try:
            workflow = run_blocking(self.engine.run(prompt))
        except Exception as exc:
            self.last_error = str(exc)
            self.state = AgentState.FAILED
            return f"AIRVIS Native Agent 오류: {exc}"

        self._workflow_id = workflow.workflow_id
        answer = workflow.output or workflow.error or "결과가 없습니다."
        session.messages.append({"role": "assistant", "content": answer})
        self.sessions._save()

        if workflow.status is WorkflowStatus.COMPLETED:
            self.state = AgentState.COMPLETED
        elif workflow.status is WorkflowStatus.CANCELLED:
            self.state = AgentState.CANCELLED
        else:
            self.state = AgentState.FAILED
            self.last_error = workflow.error
        self.costs.record(self.provider.id, getattr(self.provider, "default_model", ""), rate_per_million=0.0)
        return answer

    def run_workflow(self, prompt: str) -> dict[str, Any]:
        """V6 addition: the full structured workflow result."""
        return run_blocking(self.engine.run(prompt)).to_dict()

    # -- tools -----------------------------------------------------------------

    def execute_tool(self, name: str, arguments: dict[str, Any], confirm: bool = False) -> Any:
        if self.cancel_event.is_set():
            self.state = AgentState.CANCELLED
            raise RuntimeError("Agent task cancelled")
        if self.tool_calls >= self.max_tool_calls:
            self.state = AgentState.FAILED
            raise RuntimeError("Maximum tool calls exceeded")
        self.tool_calls += 1
        self.state = AgentState.EXECUTING
        try:
            result = run_blocking(
                self.engine.tools.call(
                    name,
                    arguments,
                    confirm=confirm,
                    approval_handler=always_approve if confirm else None,
                )
            )
            self.state = AgentState.COMPLETED
            return result.unwrap()
        except ApprovalRequiredError:
            self.state = AgentState.WAITING_CONFIRMATION
            raise
        except PermissionDeniedError:
            self.state = AgentState.WAITING_CONFIRMATION
            raise
        except Exception as exc:
            self.state = AgentState.FAILED
            self.last_error = str(exc)
            raise

    # -- tasks -----------------------------------------------------------------

    def create_task(self, prompt: str) -> Task:
        task = Task(description=prompt.strip(), name=prompt.strip()[:60])
        self.tasks[task.id] = task
        self._persist_tasks()
        return task

    def run_task(self, task_id: str) -> str:
        task = self.tasks[task_id]
        task.status = TaskStatus.RUNNING
        self._persist_tasks()
        try:
            result = self.run(task.description)
        except Exception:
            task.status = TaskStatus.FAILED
            self._persist_tasks()
            raise
        task.status = TaskStatus.COMPLETED if self.state is AgentState.COMPLETED else TaskStatus.FAILED
        self._persist_tasks()
        return result

    def task_list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": task.id,
                "prompt": task.description,
                "status": task.status.value,
                "retry_count": task.retry_count,
            }
            for task in self.tasks.values()
        ]

    def _persist_tasks(self) -> None:
        self.task_store.save(
            {
                task_id: {
                    "id": task.id,
                    "prompt": task.description,
                    "status": task.status.value,
                    "retry_count": task.retry_count,
                }
                for task_id, task in self.tasks.items()
            }
        )

    def _restore_tasks(self) -> None:
        for task_id, data in self.task_store.load().items():
            try:
                task = Task(description=str(data["prompt"]), id=task_id, status=TaskStatus(data.get("status", "queued")))
                task.attempts = int(data.get("retry_count", 0)) + 1 if data.get("retry_count") else 0
                self.tasks[task.id] = task
            except (KeyError, TypeError, ValueError):
                continue

    # -- scheduling ------------------------------------------------------------

    def schedule_once(self, prompt: str, delay_seconds: float) -> str:
        return self.scheduler.once(prompt, delay_seconds, self.run).id

    def cancel_job(self, job_id: str) -> bool:
        return self.scheduler.cancel(job_id)

    def scheduled_jobs(self) -> list[dict[str, Any]]:
        return self.scheduler.list()

    # -- lifecycle -------------------------------------------------------------

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._workflow_id:
            self.engine.cancel(self._workflow_id)
        self.state = AgentState.CANCELLED

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "session": self.session_id,
            "provider": self.provider.id,
            "providers": self.engine.providers.names(),
            "backends": self.engine.backends.names(),
            "agents": self.engine.agents.names(),
            "router": self.router.status(),
            "tasks": len(self.tasks),
            "cost_total": self.costs.total,
            "workflow": self._workflow_id,
            "error": self.last_error,
        }

    def health(self) -> dict[str, Any]:
        return run_blocking(self.engine.health_check())


def providers_from_environment() -> list[Provider]:
    """V4 helper retained for compatibility; prefer ``build_provider_registry``."""
    from .providers.factory import build_provider_registry

    return list(build_provider_registry(AirvisConfig.load()))


__all__ = [
    "AgentRuntime",
    "AgentState",
    "AnthropicProvider",
    "GeminiProvider",
    "MemoryStore",
    "MockProvider",
    "OpenAICompatibleProvider",
    "PermissionError",
    "Provider",
    "RiskLevel",
    "Tool",
    "ToolRegistry",
    "command_risk",
    "provider_from_environment",
    "providers_from_environment",
]
