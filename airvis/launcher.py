"""Human-facing AIRVIS CLI.

The launcher is intentionally thin: it provides the product-level command
surface and delegates orchestration to the real AIRVIS engine.  It follows the
Hermes-style convention that a bare ``airvis`` starts an interactive session,
while setup/model/tools/gateway are explicit operational commands.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from typing import Any

COMMANDS = {
    "status", "health", "doctor", "version", "providers", "backends", "agents", "tools",
    "models", "workflow", "task", "plan", "chat", "agent", "tool", "memory", "plugins",
    "plugin", "skills", "skill", "tasks", "logs", "costs", "schedule", "config", "setup",
    "gateway", "start", "stop", "restart", "server", "model",
}


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "-"
    return str(value)


def _print_mapping(data: dict[str, Any], indent: int = 0) -> None:
    prefix = " " * indent
    for key, value in data.items():
        label = str(key).replace("_", " ").title()
        if isinstance(value, dict):
            print(f"{prefix}{label}")
            _print_mapping(value, indent + 2)
        elif isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
            print(f"{prefix}{label}: {', '.join(_value(item) for item in value) or '-'}")
        else:
            print(f"{prefix}{label}: {_value(value)}")


def render(payload: Any, command: str) -> None:
    if command == "status" and isinstance(payload, dict):
        print("AIRVIS")
        print("────────────────────────────────────────")
        print(f"Workspace    {payload.get('workspace', '-')}")
        print(f"Strategy     {payload.get('routing_strategy', '-')}")
        print(f"Planner      {payload.get('planner', '-')}")
        for key in ("providers", "backends", "agents", "tools", "skills", "plugins"):
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, list):
                print(f"{key.title():<12} {len(value)} registered")
            elif isinstance(value, dict):
                print(f"{key.title():<12} {len(value)} configured")
        return
    if command in {"providers", "backends", "agents", "tools", "models", "plugins", "skills"} and isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name") or item.get("provider") or item.get("model")
                detail = item.get("description") or item.get("model") or item.get("state")
                print(f"✓ {name or 'item'}" + (f" — {detail}" if detail else ""))
            else:
                print(f"✓ {item}")
        return
    if isinstance(payload, dict):
        _print_mapping(payload)
    elif isinstance(payload, list):
        for item in payload:
            print(f"- {_value(item)}")
    else:
        print(_value(payload))


def _skills_command(args: list[str]) -> int:
    from .skills import SkillRegistry

    registry = SkillRegistry()
    action = args[1] if len(args) > 1 else "list"
    if action == "list":
        render(registry.list(), "skills")
        return 0
    if action == "create" and len(args) >= 3:
        print(registry.create(args[2]))
        return 0
    if action in {"enable", "disable"} and len(args) >= 3:
        if not registry.enable(args[2], action == "enable"):
            print(f"skill not found: {args[2]}", file=sys.stderr)
            return 2
        print("updated")
        return 0
    if action == "remove" and len(args) >= 3:
        if not registry.remove(args[2]):
            print(f"skill not found: {args[2]}", file=sys.stderr)
            return 2
        print("removed")
        return 0
    print("usage: airvis skills [list|create|enable|disable|remove] [name]", file=sys.stderr)
    return 2


def _interactive_chat() -> int:
    """Run the real native orchestration engine as the default CLI session."""
    from .core.config import AirvisConfig
    from .engine import AirvisEngine

    engine = AirvisEngine(AirvisConfig.load(search_from=None))
    if engine.providers.names() == ["mock"] or not [p for p in engine.providers.names() if p != "mock"]:
        print("AIRVIS is not configured with a real provider.")
        print("Run `airvis setup` first, then start again.")
        return 2

    print("AIRVIS — native orchestration")
    print("Type /help for commands, /exit to quit.\n")
    try:
        while True:
            try:
                message = input("you › ").strip()
            except EOFError:
                print()
                break
            if not message:
                continue
            if message in {"/exit", "/quit"}:
                break
            if message == "/help":
                print("/status  show engine status\n/health  check providers/backends\n/exit    quit")
                continue
            if message == "/status":
                render(engine.describe(), "status")
                continue
            if message == "/health":
                from .core.asyncutil import run_blocking
                render(run_blocking(engine.health_check()), "health")
                continue
            try:
                result = engine.run_sync(message)
                print(f"\nairvis › {result.output}\n")
            except KeyboardInterrupt:
                print("\n(interrupted)")
            except Exception as exc:
                print(f"AIRVIS error: {exc}", file=sys.stderr)
                return 1
    finally:
        try:
            from .core.asyncutil import run_blocking
            run_blocking(engine.close())
        except Exception:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Product-level global flags, matching the conventional CLI experience.
    if args in (["--version"], ["-V"]):
        from . import __version__
        print(__version__)
        return 0

    # Bare AIRVIS is the interactive native agent, not argparse help.
    if not args:
        return _interactive_chat()

    # Hermes-style operational aliases.
    if args[0] == "model":
        args = ["setup", "model", *args[1:]]
    elif args[0] == "tools" and len(args) == 1:
        args = ["tools", "list"]

    if args[0] == "setup":
        from .setup import run
        return run(args[1] if len(args) > 1 else None)

    if args[0] in {"skills", "skill"}:
        return _skills_command(args)

    if args[0] == "gateway":
        # Gateway is the long-running voice/channel process. The gateway owns
        # channel delivery; orchestration remains the native AIRVIS engine.
        args = ["server", *args[1:]]

    # Explicit chat with no prompt enters the same interactive session.
    if args[0] == "chat" and len(args) == 1:
        return _interactive_chat()

    if "--json" in args:
        from .cli import main as cli_main
        return cli_main(args)

    command = next((item for item in args if item in COMMANDS), "")
    from .cli import main as cli_main
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli_main(args)
    text = output.getvalue()
    if not text.strip():
        return code
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        print(text, end="")
        return code
    render(payload, command)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
