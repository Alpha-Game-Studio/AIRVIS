from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import socket
import time


ROOT = Path(__file__).resolve().parent.parent
PID_PATH = Path.home() / ".airvis" / "web_server.pid"

from .runtime import AgentRuntime, PermissionError
from .planning import TaskStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="airvis", description="AIRVIS native agent runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    providers = subparsers.add_parser("providers")
    providers.add_argument("action", nargs="?", choices=("list", "test"), default="list")
    models = subparsers.add_parser("models")
    models.add_argument("--local", action="store_true")
    subparsers.add_parser("tools")
    subparsers.add_parser("agents")
    plugins = subparsers.add_parser("plugins")
    plugins.add_argument("action", nargs="?", choices=("list",), default="list")
    tasks = subparsers.add_parser("tasks")
    tasks.add_argument("action", nargs="?", choices=("list", "run", "cancel"), default="list")
    tasks.add_argument("id", nargs="?")
    subparsers.add_parser("doctor")
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    subparsers.add_parser("restart")
    subparsers.add_parser("server")
    agent = subparsers.add_parser("agent")
    agent.add_argument("action", choices=("run",))
    agent.add_argument("message")
    memory = subparsers.add_parser("memory")
    memory.add_argument("action", nargs="?", choices=("list", "delete"), default="list")
    memory.add_argument("id", nargs="?")
    subparsers.add_parser("logs")
    subparsers.add_parser("costs")
    subparsers.add_parser("schedule")
    chat = subparsers.add_parser("chat")
    chat.add_argument("message")
    tool = subparsers.add_parser("tool")
    tool.add_argument("name")
    tool.add_argument("arguments", nargs="?", default="{}")
    tool.add_argument("--confirm", action="store_true")
    plugin = subparsers.add_parser("plugin")
    plugin.add_argument("action", choices=("create", "remove", "enable", "disable"))
    plugin.add_argument("name")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command in {"start", "server"}:
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        if PID_PATH.is_file():
            print("AIRVIS server may already be running")
            return 0
        process = subprocess.Popen([sys.executable, str(ROOT / "web_server.py")], cwd=ROOT, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PID_PATH.write_text(str(process.pid), encoding="utf-8")
        if not _wait_for_server():
            PID_PATH.unlink(missing_ok=True)
            print("AIRVIS server failed to start")
            return 1
        print(f"AIRVIS server started: {process.pid}")
        return 0
    if args.command == "stop":
        if not PID_PATH.is_file():
            print("AIRVIS server is not running")
            return 0
        try:
            os.kill(int(PID_PATH.read_text()), signal.SIGTERM)
            PID_PATH.unlink(missing_ok=True)
            print("AIRVIS server stopped")
        except (OSError, ValueError) as exc:
            print(f"AIRVIS stop error: {exc}")
            return 1
        return 0
    if args.command == "restart":
        if PID_PATH.is_file():
            try:
                os.kill(int(PID_PATH.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
            PID_PATH.unlink(missing_ok=True)
        process = subprocess.Popen([sys.executable, str(ROOT / "web_server.py")], cwd=ROOT, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(process.pid), encoding="utf-8")
        if not _wait_for_server():
            PID_PATH.unlink(missing_ok=True)
            print("AIRVIS server failed to restart")
            return 1
        print(f"AIRVIS server restarted: {process.pid}")
        return 0
    runtime = AgentRuntime()
    if args.command == "status":
        print(json.dumps(runtime.status(), ensure_ascii=False, indent=2))
    elif args.command == "providers":
        if args.action == "test":
            try:
                print(runtime.provider_manager.chat([{"role": "user", "content": "Reply with OK"}], []))
            except RuntimeError as exc:
                print(f"provider test failed: {exc}")
                return 1
        else:
            print(json.dumps(runtime.provider_manager.list(), ensure_ascii=False, indent=2))
    elif args.command == "models":
        print(json.dumps(runtime.catalog.list(local=True if args.local else None), ensure_ascii=False, indent=2))
    elif args.command == "tools":
        print(json.dumps(runtime.tools.list(), ensure_ascii=False, indent=2))
    elif args.command == "agents":
        print(json.dumps(runtime.agents.list(), ensure_ascii=False, indent=2))
    elif args.command == "plugins":
        print(json.dumps(runtime.plugins.list(), ensure_ascii=False, indent=2))
    elif args.command == "tasks":
        if args.action == "list":
            print(json.dumps(runtime.task_list(), ensure_ascii=False, indent=2))
        elif not args.id or args.id not in runtime.tasks:
            print("task id is required")
            return 2
        elif args.action == "run":
            print(runtime.run_task(args.id))
        else:
            runtime.tasks[args.id].status = TaskStatus.CANCELLED
            runtime._persist_tasks()
            print("cancelled")
    elif args.command == "doctor":
        from .doctor import run_checks
        print(json.dumps(run_checks(), ensure_ascii=False, indent=2))
    elif args.command == "agent":
        print(runtime.run(args.message))
    elif args.command == "memory":
        if args.action == "list":
            print(json.dumps(runtime.memory.list(), ensure_ascii=False, indent=2))
        elif args.id and runtime.memory.delete(args.id):
            print("deleted")
        else:
            print("memory id is required or was not found")
            return 2
    elif args.command == "logs":
        path = Path.home() / ".airvis" / "logs" / "audit.jsonl"
        print(path.read_text(encoding="utf-8") if path.is_file() else "No audit log")
    elif args.command == "costs":
        print(json.dumps({"total": runtime.costs.total}, ensure_ascii=False, indent=2))
    elif args.command == "schedule":
        print(json.dumps(runtime.scheduled_jobs(), ensure_ascii=False, indent=2))
    elif args.command == "plugin":
        if args.action == "create":
            print(runtime.plugins.create(args.name))
        elif args.action == "remove":
            print("removed" if runtime.plugins.remove(args.name) else "not found")
        else:
            print("updated" if runtime.plugins.enable(args.name, args.action == "enable") else "not found")
    elif args.command == "chat":
        print(runtime.run(args.message))
    elif args.command == "tool":
        try:
            print(json.dumps(runtime.execute_tool(args.name, json.loads(args.arguments), args.confirm), ensure_ascii=False, indent=2))
        except (ValueError, KeyError, PermissionError) as exc:
            print(f"AIRVIS tool error: {exc}")
            return 2
    return 0


def _wait_for_server(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
