"""Human-facing entry point for AIRVIS."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from typing import Any

COMMANDS = {"status", "health", "doctor", "version", "providers", "backends", "agents", "tools", "models", "workflow", "task", "plan", "chat", "agent", "tool", "memory", "plugins", "plugin", "tasks", "logs", "costs", "schedule", "config", "setup", "gateway", "start", "stop", "restart", "server"}


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
        print("AIRVIS 8.2")
        print("────────────────────────────────────────")
        for key in ("providers", "backends", "agents", "tools"):
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, list):
                print(f"{key.title():<12} {len(value)} registered")
            elif isinstance(value, dict):
                print(f"{key.title():<12} {len(value)} configured")
        return
    if command in {"providers", "backends", "agents", "tools", "models", "plugins"} and isinstance(payload, list):
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"] or args == ["-V"]:
        from . import __version__
        print(__version__)
        return 0
    if args and args[0] == "setup":
        from .setup import run
        return run()
    if args and args[0] == "gateway":
        # Gateway is the long-running voice/channel entry point. The current
        # HTTP gateway is exposed by the existing server manager; keeping this
        # alias stable lets the voice/channel layer evolve independently.
        args = ["server", *args[1:]]
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
