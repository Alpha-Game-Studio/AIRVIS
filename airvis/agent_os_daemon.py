"""Persistent AIRVIS Agent OS daemon.

Jobs are durable JSON files in ~/.airvis/queue and results in ~/.airvis/jobs.
Run `airvis-os daemon` to keep the native agent available for background work.
"""
from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path

from .agent_os import AgentOS
from .core.config import AirvisConfig
from .engine import AirvisEngine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="airvis-os daemon")
    parser.add_argument("--workspace")
    parser.add_argument("--config")
    parser.add_argument("--poll", type=float, default=0.5)
    args = parser.parse_args(argv)

    config = AirvisConfig.load(args.config, search_from=args.workspace)
    root = Path(args.workspace or config.workspace).expanduser().resolve()
    queue = root / ".airvis" / "queue"
    results = root / ".airvis" / "jobs"
    queue.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    engine = AirvisEngine(config, workspace=root)
    runtime = AgentOS(engine, root=root)
    running = True

    def stop(*_signal: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while running:
            for item in sorted(queue.glob("*.json")):
                try:
                    payload = json.loads(item.read_text(encoding="utf-8"))
                    item.unlink()
                except (OSError, ValueError):
                    continue
                job_id = str(payload.get("job_id", item.stem))
                request = str(payload.get("request", ""))
                strategy = payload.get("strategy")
                actual = runtime.spawn(request, strategy=strategy)
                deadline = time.time() + 86400
                while running and time.time() < deadline:
                    state = runtime.job(actual)
                    if state and state["status"] in {"completed", "failed"}:
                        (results / f"{job_id}.json").write_text(
                            json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
                        )
                        break
                    time.sleep(0.1)
            time.sleep(max(0.05, args.poll))
    finally:
        runtime.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
