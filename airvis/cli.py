"""AIRVIS command line interface."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PID_PATH = Path.home() / ".airvis" / "web_server.pid"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="airvis", description="AIRVIS AI orchestration engine")
    parser.add_argument("--config", help="path to an airvis.yaml / airvis.json file")
    parser.add_argument("--workspace", help="workspace root (defaults to the current directory)")
    parser.add_argument("--json", action="store_true", help="always emit JSON")
    parser.add_argument("--approve", action="store_true", help="auto-approve high-risk tool calls")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (("status", "engine status"), ("health", "live provider/backend health check"), ("doctor", "diagnose the installation"), ("version", "print the AIRVIS version")):
        sub.add_parser(command, help=help_text)
    providers = sub.add_parser("providers", help="inspect providers")
    providers.add_argument("action", nargs="?", choices=("list", "test", "health"), default="list")
    backends = sub.add_parser("backends", help="inspect backends")
    backends.add_argument("action", nargs="?", choices=("list", "health"), default="list")
    agents = sub.add_parser("agents", help="inspect agents")
    agents.add_argument("action", nargs="?", choices=("list", "show", "route"), default="list")
    agents.add_argument("target", nargs="?")
    tools = sub.add_parser("tools", help="inspect tools")
    tools.add_argument("action", nargs="?", choices=("list", "show"), default="list")
    tools.add_argument("name", nargs="?")
    models = sub.add_parser("models", help="list models")
    models.add_argument("--local", action="store_true")
    plugins = sub.add_parser("plugins", help="inspect installed plugins")
    plugins.add_argument("action", nargs="?", choices=("list",), default="list")
    skills = sub.add_parser("skills", help="inspect installed skills")
    skills.add_argument("action", nargs="?", choices=("list", "create", "enable", "disable"), default="list")
    skills.add_argument("name", nargs="?")
    # Keep the existing workflow/task/tool/memory/plugin/server command surface.
    workflow = sub.add_parser("workflow", help="run and inspect workflows")
    workflow.add_argument("action", choices=("run", "status", "cancel", "list", "resume", "events"))
    workflow.add_argument("target", nargs="?")
    workflow.add_argument("--strategy")
    workflow.add_argument("--approve", action="store_true", default=argparse.SUPPRESS)
    task = sub.add_parser("task", help="inspect a task")
    task.add_argument("action", nargs="?", choices=("inspect", "list", "run", "cancel"), default="list")
    task.add_argument("id", nargs="?")
    plan = sub.add_parser("plan", help="show the plan for a request without executing it")
    plan.add_argument("request")
    chat = sub.add_parser("chat", help="run a request through the pipeline")
    chat.add_argument("message")
    chat.add_argument("--approve", action="store_true", default=argparse.SUPPRESS)
    agent = sub.add_parser("agent", help="alias of 'chat'")
    agent.add_argument("action", choices=("run",))
    agent.add_argument("message")
    tool = sub.add_parser("tool", help="execute a single tool")
    tool.add_argument("name")
    tool.add_argument("arguments", nargs="?", default="{}")
    tool.add_argument("--confirm", action="store_true")
    memory = sub.add_parser("memory")
    memory.add_argument("action", nargs="?", choices=("list", "add", "delete"), default="list")
    memory.add_argument("value", nargs="?")
    plugin = sub.add_parser("plugin")
    plugin.add_argument("action", choices=("create", "remove", "enable", "disable"))
    plugin.add_argument("name")
    for command in ("tasks", "logs", "costs", "schedule", "config", "start", "stop", "restart", "server"):
        sub.add_parser(command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"start", "server", "stop", "restart"}:
        return _server_command(args.command)
    if args.command == "version":
        from . import __version__
        print(__version__)
        return 0
    from .core.config import AirvisConfig
    from .engine import AirvisEngine
    try:
        config = AirvisConfig.load(args.config, search_from=args.workspace)
    except Exception as exc:
        print(f"AIRVIS config error: {exc}", file=sys.stderr)
        return 2
    if args.command == "config":
        _emit(config.to_dict())
        return 0
    if args.command == "skills":
        from .skills import SkillRegistry
        registry = SkillRegistry()
        if args.action == "create":
            if not args.name:
                print("skill name is required", file=sys.stderr); return 2
            print(registry.create(args.name)); return 0
        if args.action in {"enable", "disable"}:
            if not args.name:
                print("skill name is required", file=sys.stderr); return 2
            print("updated" if registry.enable(args.name, args.action == "enable") else "not found"); return 0
        _emit(registry.list()); return 0
    from .security.permissions import always_approve
    approve = bool(getattr(args, "approve", False))
    engine = AirvisEngine(config, workspace=args.workspace, approval_handler=always_approve if approve else None)
    handlers = {"status": _status, "health": _health, "doctor": _doctor, "providers": _providers, "backends": _backends, "agents": _agents, "tools": _tools, "models": _models, "workflow": _workflow, "task": _task, "plan": _plan, "chat": _chat, "agent": _chat, "tool": _tool, "memory": _memory, "plugins": _plugins, "plugin": _plugin, "tasks": _legacy_tasks, "logs": _logs, "costs": _costs, "schedule": _schedule}
    handler = handlers.get(args.command)
    if handler is None:
        print(f"unknown command: {args.command}", file=sys.stderr); return 2
    return handler(engine, args)


def _status(engine: Any, args: Any) -> int:
    _emit(engine.describe()); return 0


def _health(engine: Any, args: Any) -> int:
    from .core.asyncutil import run_blocking
    report = run_blocking(engine.health_check())
    _emit(report)
    unhealthy = [name for section in ("providers", "backends") for name, status in report.get(section, {}).items() if isinstance(status, dict) and status.get("state") == "unhealthy"]
    return 1 if unhealthy else 0


def _doctor(engine: Any, args: Any) -> int:
    from .doctor import run_checks, summarize
    report = summarize(run_checks(engine)); _emit(report); return 0 if report["ok"] else 1


def _providers(engine: Any, args: Any) -> int:
    if args.action == "test":
        from .core.asyncutil import run_blocking
        from .providers.base import GenerationRequest, Message
        try:
            result = run_blocking(engine.providers.generate(GenerationRequest(messages=[Message("user", "Reply with OK")])) )
        except Exception as exc:
            print(f"provider test failed: {exc}", file=sys.stderr); return 1
        _emit({"provider": result.provider, "model": result.model, "text": result.text}); return 0
    if args.action == "health":
        from .core.asyncutil import run_blocking
        _emit(run_blocking(engine.providers.health_check_all())); return 0
    _emit(engine.providers.list()); return 0


def _backends(engine: Any, args: Any) -> int:
    if args.action == "health":
        from .core.asyncutil import run_blocking
        _emit(run_blocking(engine.backends.health_check_all())); return 0
    _emit(engine.backends.list()); return 0


def _agents(engine: Any, args: Any) -> int:
    if args.action == "show":
        if not args.target: print("agent id is required", file=sys.stderr); return 2
        _emit(engine.agents.get(args.target).to_dict()); return 0
    if args.action == "route":
        if not args.target: print("a task description is required", file=sys.stderr); return 2
        from .orchestration.task import Task
        _emit([item.to_dict() for item in engine.router.rank(Task(description=args.target))]); return 0
    _emit(engine.agents.list()); return 0


def _tools(engine: Any, args: Any) -> int:
    if args.action == "show":
        if not args.name: print("tool name is required", file=sys.stderr); return 2
        _emit(engine.tools.get(args.name).schema()); return 0
    _emit(engine.tools.list()); return 0


def _models(engine: Any, args: Any) -> int:
    from .models import ModelCatalog
    _emit(ModelCatalog(engine.providers).list(local=True if args.local else None)); return 0


def _plan(engine: Any, args: Any) -> int:
    from .core.asyncutil import run_blocking
    _emit(run_blocking(engine.planner.plan(args.request)).to_dict()); return 0


def _workflow(engine: Any, args: Any) -> int:
    from .core.asyncutil import run_blocking
    if args.action == "run":
        if not args.target: print("a request is required", file=sys.stderr); return 2
        result = run_blocking(engine.run(args.target, strategy=args.strategy)); _emit(result.to_dict()); return 0 if result.ok else 1
    if args.action == "status":
        if not args.target: print("a workflow id is required", file=sys.stderr); return 2
        _emit(engine.orchestrator.status(args.target)); return 0
    if args.action == "cancel":
        if not args.target: print("a workflow id is required", file=sys.stderr); return 2
        _emit({"cancelled": engine.cancel(args.target)}); return 0
    if args.action == "resume":
        if not args.target: print("a workflow id is required", file=sys.stderr); return 2
        result = run_blocking(engine.resume(args.target)); _emit(result.to_dict()); return 0 if result.ok else 1
    if args.action == "events": _emit(engine.store.list_events(args.target)); return 0
    _emit(engine.store.list_workflows()); return 0


def _task(engine: Any, args: Any) -> int:
    if args.action == "inspect":
        if not args.id: print("a task id is required", file=sys.stderr); return 2
        record = engine.store.load_task(args.id)
        if record is None: print(f"unknown task: {args.id}", file=sys.stderr); return 2
        _emit(record); return 0
    _emit(engine.store.list_workflows()); return 0


def _chat(engine: Any, args: Any) -> int:
    from .core.asyncutil import run_blocking
    result = run_blocking(engine.run(args.message)); print(result.output); return 0 if result.ok else 1


def _tool(engine: Any, args: Any) -> int:
    from .core.asyncutil import run_blocking
    from .core.errors import AirvisError
    from .security.permissions import always_approve
    try: arguments = json.loads(args.arguments or "{}")
    except ValueError as exc: print(f"AIRVIS tool error: invalid JSON arguments: {exc}", file=sys.stderr); return 2
    try:
        result = run_blocking(engine.tools.call(args.name, arguments, confirm=args.confirm, approval_handler=always_approve if args.confirm else None))
    except (AirvisError, ValueError, KeyError) as exc: print(f"AIRVIS tool error: {exc}", file=sys.stderr); return 2
    _emit(result.to_dict()); return 0 if result.ok else 1


def _memory(engine: Any, args: Any) -> int:
    if args.action == "add":
        if not args.value: print("content is required", file=sys.stderr); return 2
        _emit({"id": engine.memory.add(args.value)}); return 0
    if args.action == "delete":
        if not args.value or not engine.memory.delete(args.value): print("memory id is required or was not found", file=sys.stderr); return 2
        print("deleted"); return 0
    _emit(engine.memory.list()); return 0


def _plugins(engine: Any, args: Any) -> int:
    _emit(engine.plugins.list()); return 0


def _plugin(engine: Any, args: Any) -> int:
    manager = engine.plugins
    if args.action == "create": print(manager.create(args.name))
    elif args.action == "remove": print("removed" if manager.remove(args.name) else "not found")
    else: print("updated" if manager.enable(args.name, args.action == "enable") else "not found")
    return 0


def _legacy_tasks(engine: Any, args: Any) -> int:
    from .runtime import AgentRuntime
    runtime = AgentRuntime(engine.workspace)
    if args.action == "list": _emit(runtime.task_list()); return 0
    if not args.id or args.id not in runtime.tasks: print("task id is required", file=sys.stderr); return 2
    if args.action == "run": print(runtime.run_task(args.id)); return 0
    from .orchestration.task import TaskStatus
    runtime.tasks[args.id].status = TaskStatus.CANCELLED; runtime._persist_tasks(); print("cancelled"); return 0


def _logs(engine: Any, args: Any) -> int:
    path = Path.home() / ".airvis" / "logs" / "audit.jsonl"; print(path.read_text(encoding="utf-8") if path.is_file() else "No audit log"); return 0


def _costs(engine: Any, args: Any) -> int:
    from .costs import CostTracker
    _emit({"total": CostTracker().total}); return 0


def _schedule(engine: Any, args: Any) -> int:
    from .scheduler import Scheduler
    _emit(Scheduler().list()); return 0


def _server_command(command: str) -> int:
    if command in {"start", "server"}:
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        if PID_PATH.is_file(): print("AIRVIS server may already be running"); return 0
        process = _spawn_server(); PID_PATH.write_text(str(process.pid), encoding="utf-8")
        if not _wait_for_server(): PID_PATH.unlink(missing_ok=True); print("AIRVIS server failed to start", file=sys.stderr); return 1
        print(f"AIRVIS server started: {process.pid}"); return 0
    if command == "stop":
        if not PID_PATH.is_file(): print("AIRVIS server is not running"); return 0
        try: os.kill(int(PID_PATH.read_text()), signal.SIGTERM); PID_PATH.unlink(missing_ok=True); print("AIRVIS server stopped")
        except (OSError, ValueError) as exc: print(f"AIRVIS stop error: {exc}", file=sys.stderr); return 1
        return 0
    if PID_PATH.is_file():
        with contextlib.suppress(OSError, ValueError): os.kill(int(PID_PATH.read_text()), signal.SIGTERM)
        PID_PATH.unlink(missing_ok=True)
    process = _spawn_server(); PID_PATH.parent.mkdir(parents=True, exist_ok=True); PID_PATH.write_text(str(process.pid), encoding="utf-8")
    if not _wait_for_server(): PID_PATH.unlink(missing_ok=True); print("AIRVIS server failed to restart", file=sys.stderr); return 1
    print(f"AIRVIS server restarted: {process.pid}"); return 0


def _spawn_server() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(ROOT / "web_server.py")], cwd=ROOT, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for_server(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout; port = int(os.environ.get("AIRVIS_PORT", "8765"))
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2): return True
        except OSError: time.sleep(0.05)
    return False


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
