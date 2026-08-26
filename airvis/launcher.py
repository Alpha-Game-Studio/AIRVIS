"""Human-facing AIRVIS CLI."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from typing import Any

COMMANDS = {"status", "health", "doctor", "version", "providers", "backends", "agents", "tools", "models", "workflow", "task", "plan", "chat", "agent", "tool", "memory", "plugins", "plugin", "skills", "skill", "tasks", "logs", "costs", "schedule", "config", "setup", "gateway", "start", "stop", "restart", "server", "model"}


def _value(value: Any) -> str:
    if isinstance(value, bool): return "yes" if value else "no"
    if value is None: return "-"
    return str(value)


def _print_mapping(data: dict[str, Any], indent: int = 0) -> None:
    prefix = " " * indent
    for key, value in data.items():
        label = str(key).replace("_", " ").title()
        if isinstance(value, dict):
            print(f"{prefix}{label}"); _print_mapping(value, indent + 2)
        elif isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
            print(f"{prefix}{label}: {', '.join(_value(item) for item in value) or '-'}")
        else: print(f"{prefix}{label}: {_value(value)}")


def render(payload: Any, command: str) -> None:
    if command == "status" and isinstance(payload, dict):
        from . import __version__
        print(f"AIRVIS {__version__.rsplit('.', 1)[0]}\n────────────────────────────────────────")
        print(f"Workspace    {payload.get('workspace', '-')}")
        print(f"Strategy     {payload.get('routing_strategy', '-')}")
        print(f"Planner      {payload.get('planner', '-')}")
        for key in ("providers", "backends", "agents", "tools", "skills", "plugins"):
            if key in payload:
                value = payload[key]
                print(f"{key.title():<12} {len(value)} {'registered' if isinstance(value, list) else 'configured'}")
        return
    if command in {"providers", "backends", "agents", "tools", "models", "plugins", "skills"} and isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name") or item.get("provider") or item.get("model")
                detail = item.get("description") or item.get("model") or item.get("state")
                print(f"✓ {name or 'item'}" + (f" — {detail}" if detail else ""))
            else: print(f"✓ {item}")
        return
    if isinstance(payload, dict): _print_mapping(payload)
    elif isinstance(payload, list):
        for item in payload: print(f"- {_value(item)}")
    else: print(_value(payload))


def _human_output(value: Any) -> str:
    """Keep accidental structured model envelopes out of the human CLI."""
    if value is None: return ""
    text = str(value).strip()
    if not text or text.startswith("```"): return text
    try: payload = json.loads(text)
    except (TypeError, ValueError): return text
    if isinstance(payload, dict):
        for key in ("final_answer", "answer", "response", "message", "summary", "output", "text"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip(): return candidate.strip()
        if payload.get("tool") or payload.get("name") or payload.get("arguments"):
            return "작업 결과를 정리하는 중 문제가 발생했습니다."
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if isinstance(payload, list): return "\n".join(str(item) for item in payload)
    return text


def _configured_engine():
    """Build the actual native engine; never silently fall back to MockProvider."""
    from .core.config import AirvisConfig
    from .engine import AirvisEngine
    engine = AirvisEngine(AirvisConfig.load(search_from=None))
    if not [name for name in engine.providers.names() if name != "mock"]:
        try:
            from .core.asyncutil import run_blocking
            run_blocking(engine.close())
        except Exception: pass
        print("AIRVIS is not configured with a real provider.", file=sys.stderr)
        print("Run `airvis setup` first.", file=sys.stderr)
        return None
    return engine


def _run_chat(message: str) -> int:
    engine = _configured_engine()
    if engine is None: return 2
    try:
        result = engine.run_sync(message)
        output = _human_output(getattr(result, "output", result))
        if output: print(output)
        return 0 if getattr(result, "ok", True) else 1
    except KeyboardInterrupt:
        print("\n(interrupted)"); return 130
    except Exception as exc:
        print(f"AIRVIS error: {exc}", file=sys.stderr); return 1
    finally:
        try:
            from .core.asyncutil import run_blocking
            run_blocking(engine.close())
        except Exception: pass


def _interactive_chat() -> int:
    engine = _configured_engine()
    if engine is None: return 2
    print("AIRVIS — native orchestration")
    print("Type /help for commands, /exit to quit.\n")
    try:
        while True:
            try: message = input("you › ").strip()
            except EOFError: print(); break
            if not message: continue
            if message in {"/exit", "/quit"}: break
            if message == "/help":
                print("/status  show engine status\n/health  check providers/backends\n/exit    quit"); continue
            if message == "/status": render(engine.describe(), "status"); continue
            if message == "/health":
                from .core.asyncutil import run_blocking
                render(run_blocking(engine.health_check()), "health"); continue
            try:
                result = engine.run_sync(message)
                print(f"\nairvis › {_human_output(getattr(result, 'output', result))}\n")
            except KeyboardInterrupt: print("\n(interrupted)")
            except Exception as exc:
                print(f"AIRVIS error: {exc}", file=sys.stderr); return 1
    finally:
        try:
            from .core.asyncutil import run_blocking
            run_blocking(engine.close())
        except Exception: pass
    return 0


def _skills_command(args: list[str]) -> int:
    from .skills import SkillRegistry
    registry = SkillRegistry(); action = args[1] if len(args) > 1 else "list"
    if action == "list": render(registry.list(), "skills"); return 0
    if action == "create" and len(args) >= 3: print(registry.create(args[2])); return 0
    if action in {"enable", "disable"} and len(args) >= 3:
        if not registry.enable(args[2], action == "enable"): print(f"skill not found: {args[2]}", file=sys.stderr); return 2
        print("updated"); return 0
    if action == "remove" and len(args) >= 3:
        if not registry.remove(args[2]): print(f"skill not found: {args[2]}", file=sys.stderr); return 2
        print("removed"); return 0
    print("usage: airvis skills [list|create|enable|disable|remove] [name]", file=sys.stderr); return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--version"], ["-V"]):
        from . import __version__
        print(__version__); return 0
    if not args: return _interactive_chat()
    if args[0] == "model": args = ["setup", "model", *args[1:]]
    elif args[0] == "tools" and len(args) == 1: args = ["tools", "list"]
    if args[0] == "setup":
        from .setup import run
        return run(args[1] if len(args) > 1 else None)
    if args[0] in {"skills", "skill"}: return _skills_command(args)
    if args[0] == "gateway": args = ["server", *args[1:]]
    if args[0] == "chat": return _interactive_chat() if len(args) == 1 else _run_chat(" ".join(args[1:]))
    if "--json" in args:
        from .cli import main as cli_main
        return cli_main(args)
    command = next((item for item in args if item in COMMANDS), "")
    from .cli import main as cli_main
    output = io.StringIO()
    with contextlib.redirect_stdout(output): code = cli_main(args)
    text = output.getvalue()
    if not text.strip(): return code
    try: payload = json.loads(text)
    except json.JSONDecodeError: print(text, end=""); return code
    render(payload, command); return code


if __name__ == "__main__": raise SystemExit(main())
