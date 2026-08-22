from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from .planning import Planner, Task, TaskStatus
from .scheduler import Scheduler
from .sessions import SessionManager
from .multiagent import AgentDelegator
from .plugins import PluginManager
from .task_store import TaskStore
from .audit import AuditLog
from .model_router import ModelRouter
from .models import ModelCatalog
from .provider_manager import ProviderManager
from .costs import CostTracker
from .backends import HermesBackend, OpenClawBackend


class AgentState(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    EXECUTING = "executing"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Provider(Protocol):
    id: str
    capabilities: set[str]

    def chat(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> str: ...


@dataclass
class MockProvider:
    id: str = "mock"
    capabilities: set[str] = field(default_factory=lambda: {"chat", "tools", "local"})

    def chat(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> str:
        prompt = messages[-1]["content"] if messages else ""
        return f"Mock Provider 응답: {prompt}"


@dataclass
class OpenAICompatibleProvider:
    id: str
    base_url: str
    api_key: str = ""
    model: str = ""
    timeout: int = 30
    capabilities: set[str] = field(default_factory=lambda: {"chat", "tools"})

    def chat(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> str:
        import urllib.request

        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read())
        return str(data["choices"][0]["message"].get("content", "")).strip()


@dataclass
class AnthropicProvider:
    id: str = "anthropic"
    model: str = "claude-3-5-sonnet-latest"
    api_key: str = ""
    capabilities: set[str] = field(default_factory=lambda: {"chat", "tools"})

    def chat(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> str:
        payload: dict[str, Any] = {"model": self.model, "max_tokens": 2048, "messages": messages}
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        return "".join(str(item.get("text", "")) for item in data.get("content", [])).strip()


@dataclass
class GeminiProvider:
    id: str = "gemini"
    model: str = "gemini-2.0-flash"
    api_key: str = ""
    capabilities: set[str] = field(default_factory=lambda: {"chat", "tools", "vision"})

    def chat(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> str:
        contents = [{"role": "user" if item["role"] == "user" else "model", "parts": [{"text": item["content"]}]} for item in messages]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={urllib.parse.quote(self.api_key)}"
        request = urllib.request.Request(url, data=json.dumps({"contents": contents}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        return str(data["candidates"][0]["content"]["parts"][0].get("text", "")).strip()


def provider_from_environment() -> Provider:
    provider_id = os.environ.get("AIRVIS_PROVIDER", "mock").strip().lower()
    if provider_id in {"ollama", "local"}:
        return OpenAICompatibleProvider("ollama", os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434") + "/v1", "ollama", os.environ.get("OLLAMA_MODEL", "llama3.2"))
    if provider_id == "anthropic":
        return AnthropicProvider(api_key=os.environ.get("ANTHROPIC_API_KEY", ""), model=os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"))
    if provider_id == "gemini":
        return GeminiProvider(api_key=os.environ.get("GOOGLE_API_KEY", ""), model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"))
    if provider_id in {"openai", "xai", "openrouter", "custom"}:
        defaults = {"openai": "https://api.openai.com/v1", "xai": "https://api.x.ai/v1", "openrouter": "https://openrouter.ai/api/v1", "custom": os.environ.get("AIRVIS_CUSTOM_BASE_URL", "http://127.0.0.1:8000/v1")}
        keys = {"openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "custom": "AIRVIS_API_KEY"}
        return OpenAICompatibleProvider(provider_id, defaults[provider_id], os.environ.get(keys[provider_id], ""), os.environ.get("AIRVIS_MODEL", "gpt-4o-mini"))
    return MockProvider()


def providers_from_environment() -> list[Provider]:
    primary = provider_from_environment()
    providers = [primary]
    fallback_id = os.environ.get("AIRVIS_FALLBACK_PROVIDER", "").strip().lower()
    if fallback_id and fallback_id != primary.id:
        previous = os.environ.get("AIRVIS_PROVIDER")
        os.environ["AIRVIS_PROVIDER"] = fallback_id
        try:
            providers.append(provider_from_environment())
        finally:
            if previous is None:
                os.environ.pop("AIRVIS_PROVIDER", None)
            else:
                os.environ["AIRVIS_PROVIDER"] = previous
    if os.environ.get("OPENCLAW_CLI"):
        providers.append(OpenClawBackend(os.environ["OPENCLAW_CLI"]))
    if os.environ.get("HERMES_CLI"):
        providers.append(HermesBackend(os.environ["HERMES_CLI"]))
    return providers


@dataclass
class Tool:
    name: str
    description: str
    risk: str
    handler: Callable[..., Any]

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "risk": self.risk}


class PermissionError(RuntimeError):
    pass


def command_risk(command: str) -> str:
    lowered = command.lower().strip()
    if any(token in lowered for token in ("sudo", "shutdown", "reboot", "rm -rf /", "disk erase")):
        return "CRITICAL"
    if any(token in lowered for token in ("rm ", "mv ", "chmod ", "git push", "pip install", "npm install")):
        return "HIGH" if any(token in lowered for token in ("rm ", "mv ", "chmod ", "git push")) else "MEDIUM"
    return "SAFE"


class ToolRegistry:
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        self.tools: dict[str, Tool] = {}
        self.register(Tool("system.info", "Return basic system information", "READ", self._system_info))
        self.register(Tool("filesystem.search", "Search filenames within the workspace", "READ", self._search))
        self.register(Tool("filesystem.read", "Read a text file in the workspace", "READ", self._read))
        self.register(Tool("filesystem.write", "Write a text file in the workspace", "MODIFY", self._write))
        self.register(Tool("filesystem.delete", "Delete a file in the workspace", "DESTRUCTIVE", self._delete))
        self.register(Tool("terminal.execute", "Run a read-only diagnostic command", "HIGH", self._terminal))
        self.register(Tool("web.fetch", "Fetch a public HTTP or HTTPS page", "NETWORK", self._web_fetch))
        self.register(Tool("browser.open", "Open a URL in the default browser", "NETWORK", self._browser_open))
        self.register(Tool("github.search", "Search public GitHub repositories", "NETWORK", self._github_search))

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def list(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    def execute(self, name: str, arguments: dict[str, Any], confirm: bool = False) -> Any:
        if name not in self.tools:
            raise KeyError(f"Unknown tool: {name}")
        tool = self.tools[name]
        if tool.risk in {"MODIFY", "HIGH", "DESTRUCTIVE"} and not confirm:
            raise PermissionError(f"Confirmation required for {name}")
        return tool.handler(**arguments)

    def _safe_path(self, path: str) -> Path:
        candidate = (self.workspace / path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise PermissionError("Path is outside the AIRVIS workspace")
        return candidate

    def _system_info(self) -> dict[str, str]:
        return {"platform": os.name, "python": os.sys.version.split()[0], "workspace": str(self.workspace)}

    def _search(self, pattern: str = "*") -> list[str]:
        return [str(p.relative_to(self.workspace)) for p in self.workspace.rglob(pattern) if p.is_file()][:200]

    def _read(self, path: str) -> str:
        target = self._safe_path(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_text(encoding="utf-8")[:20000]

    def _write(self, path: str, content: str) -> str:
        target = self._safe_path(path)
        if len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("file content exceeds 1 MB")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target.relative_to(self.workspace))

    def _delete(self, path: str) -> str:
        target = self._safe_path(path)
        if target == self.workspace or not target.is_file():
            raise PermissionError("Only existing files can be deleted")
        target.unlink()
        return str(target.relative_to(self.workspace))

    def _terminal(self, command: str) -> str:
        parts = command.strip().split()
        if command_risk(command) == "CRITICAL":
            raise PermissionError("CRITICAL commands are blocked")
        allowed = {"pwd", "ls", "git status", "python --version"}
        normalized = " ".join(parts)
        if normalized not in allowed:
            raise PermissionError("Only read-only diagnostic commands are allowed")
        result = subprocess.run(parts, cwd=self.workspace, capture_output=True, text=True, timeout=10, check=False)
        return (result.stdout or result.stderr).strip()[:20000]

    def _web_fetch(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Only HTTP(S) URLs are allowed")
        request = urllib.request.Request(url, headers={"User-Agent": "AIRVIS/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read(20000).decode("utf-8", errors="replace")

    def _browser_open(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Only HTTP(S) URLs are allowed")
        webbrowser.open(url)
        return "브라우저를 열었습니다."

    def _github_search(self, query: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(query.strip())
        request = urllib.request.Request(f"https://api.github.com/search/repositories?q={encoded}&per_page=10", headers={"Accept": "application/vnd.github+json", "User-Agent": "AIRVIS/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
        return [{"name": item.get("full_name"), "url": item.get("html_url"), "description": item.get("description")} for item in data.get("items", [])]


class MemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".airvis" / "memory.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, created REAL NOT NULL)")

    def add(self, content: str) -> str:
        memory_id = uuid.uuid4().hex
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO memories VALUES (?, ?, ?)", (memory_id, content, time.time()))
        return memory_id

    def list(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT id, content, created FROM memories ORDER BY created DESC").fetchall()
        return [{"id": row[0], "content": row[1], "created": row[2]} for row in rows]

    def delete(self, memory_id: str) -> bool:
        with sqlite3.connect(self.path) as db:
            cursor = db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0


class AgentRuntime:
    def __init__(self, workspace: Path | None = None, provider: Provider | None = None, max_iterations: int = 4, memory_path: Path | None = None, task_path: Path | None = None, audit_path: Path | None = None, session_path: Path | None = None, timeout_seconds: float = 120.0, max_tool_calls: int = 20) -> None:
        self.state = AgentState.IDLE
        self.session_id = uuid.uuid4().hex
        self.max_iterations = max(1, max_iterations)
        self.timeout_seconds = max(1.0, timeout_seconds)
        self.max_tool_calls = max(1, max_tool_calls)
        self.tool_calls = 0
        self.cancel_event = threading.Event()
        self.provider = provider or provider_from_environment()
        self.provider_manager = ProviderManager([self.provider] if provider else providers_from_environment())
        self.tools = ToolRegistry(workspace)
        self.memory = MemoryStore(memory_path)
        self.sessions = SessionManager(session_path)
        self.planner = Planner()
        self.scheduler = Scheduler()
        self.agents = AgentDelegator()
        self.plugins = PluginManager()
        self.task_store = TaskStore(task_path)
        self.audit = AuditLog(audit_path)
        self.router = ModelRouter(self.provider.id, getattr(self.provider, "model", ""))
        self.catalog = ModelCatalog()
        self.costs = CostTracker()
        self.tasks: dict[str, Task] = {}
        self._restore_tasks()
        self.messages: list[dict[str, str]] = []
        self.last_error: str | None = None

    def providers(self) -> list[dict[str, Any]]:
        return [{"id": self.provider.id, "model": getattr(self.provider, "model", ""), "capabilities": sorted(self.provider.capabilities), "status": "ready"}]

    def run(self, prompt: str) -> str:
        prompt = prompt.strip()
        if not prompt:
            return "명령을 입력해주세요."
        self.state = AgentState.THINKING
        self.cancel_event.clear()
        self.tool_calls = 0
        self.last_error = None
        self.audit.record("agent.run", session=self.session_id, prompt=prompt)
        session = self.sessions.get()
        session.messages.append({"role": "user", "content": prompt})
        self.messages = session.messages
        try:
            if self.cancel_event.is_set():
                self.state = AgentState.CANCELLED
                return "작업을 취소했습니다."
            local_answer = self._handle_local_tool_intent(prompt)
            if local_answer is not None:
                answer = local_answer
                session.messages.append({"role": "assistant", "content": answer})
                self.sessions._save()
                self.state = AgentState.COMPLETED
                self.audit.record("tool.intent", session=self.session_id, prompt=prompt)
                return answer
            memory_request = re.match(r"(?:이거|이 내용을) 기억해[:：]?\s*(.*)", prompt, re.I)
            if memory_request and memory_request.group(1):
                self.memory.add(memory_request.group(1).strip())
                answer = "기억해두었습니다."
            else:
                answer = self._agent_loop(session)
            session.messages.append({"role": "assistant", "content": answer})
            self.sessions._save()
            self.state = AgentState.COMPLETED
            return answer
        except Exception as exc:
            self.last_error = str(exc)
            self.state = AgentState.FAILED
            return f"AIRVIS Native Agent 오류: {exc}"

    def _agent_loop(self, session) -> str:
        for _ in range(self.max_iterations):
            if self.cancel_event.is_set():
                self.state = AgentState.CANCELLED
                return "작업을 취소했습니다."
            answer = self.provider_manager.chat(self.messages, self.tools.list())
            self.costs.record(self.provider.id, getattr(self.provider, "model", ""), rate_per_million=0.0)
            request = self._parse_tool_call(answer)
            if request is None:
                return answer
            name, arguments = request
            try:
                result = self.execute_tool(name, arguments)
            except PermissionError:
                return f"{name} 실행은 사용자 확인이 필요합니다."
            observation = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
            self.messages.append({"role": "assistant", "content": answer})
            self.messages.append({"role": "tool", "content": observation})
        return "AIRVIS Agent 반복 한도에 도달했습니다."

    @staticmethod
    def _parse_tool_call(answer: str) -> tuple[str, dict[str, Any]] | None:
        try:
            payload = json.loads(answer)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        tool_name = payload.get("tool") or payload.get("name")
        arguments = payload.get("arguments", payload.get("input", {}))
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return None
        return tool_name, arguments

    def _handle_local_tool_intent(self, prompt: str) -> str | None:
        lowered = prompt.lower()
        if any(word in lowered for word in ("python 버전", "파이썬 버전", "python version")):
            return str(self.execute_tool("terminal.execute", {"command": "python --version"}, confirm=True))
        if any(word in lowered for word in ("시스템 정보", "system info", "컴퓨터 정보")):
            return json.dumps(self.execute_tool("system.info", {}), ensure_ascii=False)
        if any(word in lowered for word in ("파일 찾아", "파일 검색", "search files", "find files")):
            pattern = "*.pdf" if "pdf" in lowered else "*"
            results = self.execute_tool("filesystem.search", {"pattern": pattern})
            return f"파일 {len(results)}개를 찾았습니다.\n" + "\n".join(results[:20])
        return None

    def create_task(self, prompt: str) -> Task:
        task = self.planner.create(prompt)
        self.tasks[task.id] = task
        self._persist_tasks()
        return task

    def run_task(self, task_id: str) -> str:
        task = self.tasks[task_id]
        task.status = TaskStatus.RUNNING
        try:
            result = self.run(task.prompt)
            task.status = TaskStatus.COMPLETED if self.state == AgentState.COMPLETED else TaskStatus.FAILED
            self._persist_tasks()
            return result
        except Exception:
            task.status = TaskStatus.FAILED
            self._persist_tasks()
            raise

    def _persist_tasks(self) -> None:
        self.task_store.save({task_id: {"id": task.id, "prompt": task.prompt, "status": task.status.value, "retry_count": task.retry_count} for task_id, task in self.tasks.items()})

    def _restore_tasks(self) -> None:
        for task_id, data in self.task_store.load().items():
            try:
                task = Task(prompt=str(data["prompt"]), id=task_id, status=TaskStatus(data.get("status", "queued")), retry_count=int(data.get("retry_count", 0)))
                self.tasks[task.id] = task
            except (KeyError, TypeError, ValueError):
                continue

    def task_list(self) -> list[dict[str, Any]]:
        return [{"id": task.id, "prompt": task.prompt, "status": task.status.value, "retry_count": task.retry_count} for task in self.tasks.values()]

    def schedule_once(self, prompt: str, delay_seconds: float) -> str:
        job = self.scheduler.once(prompt, delay_seconds, self.run)
        return job.id

    def cancel_job(self, job_id: str) -> bool:
        return self.scheduler.cancel(job_id)

    def scheduled_jobs(self) -> list[dict[str, Any]]:
        return self.scheduler.list()

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
            result = self.tools.execute(name, arguments, confirm)
            self.state = AgentState.COMPLETED
            return result
        except PermissionError:
            self.state = AgentState.WAITING_CONFIRMATION
            raise
        except Exception as exc:
            self.state = AgentState.FAILED
            self.last_error = str(exc)
            raise

    def cancel(self) -> None:
        self.cancel_event.set()
        self.state = AgentState.CANCELLED

    def status(self) -> dict[str, Any]:
        return {"state": self.state.value, "session": self.session_id, "provider": self.provider.id, "router": self.router.status(), "tasks": len(self.tasks), "cost_total": self.costs.total, "error": self.last_error}
