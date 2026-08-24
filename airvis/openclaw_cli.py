"""CLI for the first-class AIRVIS OpenClaw runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .openclaw import OpenClaw, OpenClawOptions
from .security.permissions import always_approve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="airvis-claw", description="AIRVIS OpenClaw — autonomous multi-agent orchestration runtime")
    parser.add_argument("--workspace", default=".", help="workspace root")
    parser.add_argument("--approve", action="store_true", help="auto-approve high-risk tools")
    parser.add_argument("--strategy", default="balanced", choices=("cheap", "balanced", "fast", "quality", "premium"))
    parser.add_argument("--provider", default=None, help="real model provider (openai, anthropic, gemini, xai, openrouter, ollama, custom)")
    parser.add_argument("--model", default=None, help="model id to use for every native agent")
    parser.add_argument("--no-llm-planner", action="store_true", help="use deterministic planner")
    parser.add_argument("--no-repair", action="store_true", help="disable automatic repair retries")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run an autonomous request")
    run.add_argument("request")
    sub.add_parser("describe", help="show runtime capabilities")
    sub.add_parser("providers", help="show configured model providers")
    resume = sub.add_parser("resume", help="resume a workflow")
    resume.add_argument("workflow_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    claw = OpenClaw(
        Path(args.workspace),
        options=OpenClawOptions(
            strategy=args.strategy,
            provider=args.provider,
            model=args.model,
            use_llm_planner=not args.no_llm_planner,
            auto_repair=not args.no_repair,
        ),
        approval_handler=always_approve if args.approve else None,
    )

    if args.command == "describe":
        _emit(claw.describe())
        return 0
    if args.command == "providers":
        _emit(claw.engine.providers.list())
        return 0
    if args.command == "resume":
        result = asyncio.run(claw.resume(args.workflow_id))
    else:
        result = claw.run_sync(args.request)
    _emit(result.to_dict())
    return 0 if result.ok else 1


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
