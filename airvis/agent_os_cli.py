"""CLI for the long-lived AIRVIS Agent OS runtime."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .agent_os import AgentOS
from .core.config import AirvisConfig
from .engine import AirvisEngine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="airvis-os", description="AIRVIS autonomous Agent OS")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    session = sub.add_parser("session")
    session.add_argument("action", choices=("list", "show", "reset"))
    session.add_argument("name", nargs="?", default="default")

    memory = sub.add_parser("memory")
    memory.add_argument("action", choices=("search", "add", "list"))
    memory.add_argument("query", nargs="?")

    spawn = sub.add_parser("spawn")
    spawn.add_argument("request")
    spawn.add_argument("--strategy")

    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("request")
    enqueue.add_argument("--strategy")

    job = sub.add_parser("job")
    job.add_argument("action", choices=("list", "show", "cancel", "children"))
    job.add_argument("id", nargs="?")

    goal = sub.add_parser("goal")
    goal.add_argument("request")
    goal.add_argument("--strategy")

    daemon = sub.add_parser("daemon", help="run the persistent background agent daemon")
    daemon.add_argument("--poll", type=float, default=0.5)

    args = parser.parse_args(argv)
    config = AirvisConfig.load(args.config, search_from=args.workspace)
    root = Path(args.workspace or config.workspace).expanduser().resolve()

    if args.command == "daemon":
        command = [sys.executable, "-m", "airvis.agent_os_daemon", "--workspace", str(root)]
        if args.config:
            command += ["--config", args.config]
        command += ["--poll", str(args.poll)]
        return subprocess.call(command)

    if args.command == "enqueue":
        import uuid
        queue = root / ".airvis" / "queue"
        queue.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        (queue / f"{job_id}.json").write_text(
            json.dumps({"job_id": job_id, "request": args.request, "strategy": args.strategy}, ensure_ascii=False),
            encoding="utf-8",
        )
        return emit({"job_id": job_id, "queued": True, "queue": str(queue)})

    engine = AirvisEngine(config, workspace=root)
    os = AgentOS(engine, root=root)
    try:
        if args.command == "session":
            if args.action == "list":
                return emit(os.sessions_list())
            if args.action == "show":
                return emit(os.session(args.name))
            return 0 if os.reset_session(args.name) else 1
        if args.command == "memory":
            if args.action == "add":
                if not args.query:
                    print("memory content is required", file=sys.stderr)
                    return 2
                return emit({"id": os.remember(args.query)})
            if args.action == "search":
                return emit(os.recall(args.query or ""))
            return emit(os.memory.list())
        if args.command == "spawn":
            return emit({"job_id": os.spawn(args.request, strategy=args.strategy)})
        if args.command == "job":
            if args.action == "list":
                return emit(os.jobs())
            if not args.id:
                print("job id is required", file=sys.stderr)
                return 2
            if args.action == "show":
                return emit(os.job(args.id))
            if args.action == "children":
                return emit(os.children(args.id))
            return 0 if os.cancel_job(args.id) else 1
        if args.command == "goal":
            print(os.run_goal(args.request, strategy=args.strategy))
            return 0
        return 2
    finally:
        os.shutdown()


def emit(value: object) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
