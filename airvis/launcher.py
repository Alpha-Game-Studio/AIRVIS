"""Human-facing AIRVIS CLI.

The command surface follows the ergonomics of modern agent CLIs while keeping
AIRVIS's native orchestrator as the execution authority.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

COMMANDS = {
    "status", "health", "doctor", "version", "providers", "backends", "agents", "tools", "models",
    "workflow", "task", "plan", "chat", "agent", "tool", "memory", "plugins", "plugin", "skills", "skill",
    "tasks", "logs", "costs", "schedule", "config", "setup", "gateway", "start", "stop", "restart", "server", "model",
    "init", "list", "add", "remove", "voice", "actions", "flow",
}

SETUP_PATH = Path.home() / ".airvis" / "setup.json"
WORKSPACE_CONFIG = Path.cwd() / "airvis.json"


def _value(value: Any) -> str:
    if isinstance(value, bool): return "yes" if value else "no"
    if value is None: return "-"
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
            else:
                print(f"✓ {item}")
        return
    if isinstance(payload, dict):
        _print_mapping(payload)
    elif isinstance(payload, list):
        for item in payload: print(f"- {_value(item)}")
    else:
        print(_value(payload))


def _human_output(value: Any) -> str:
    if value is None: return ""
    text = str(value).strip()
    if not text or text.startswith("```"): return text
    try: payload = json.loads(text)
    except (TypeError, ValueError): return text
    if isinstance(payload, dict):
        for key in ("final_answer", "answer", "response", "message", "summary", "output", "text"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip(): return candidate.strip()
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if isinstance(payload, list): return "\n".join(str(item) for item in payload)
    return text


def _configured_engine():
    """Build the native engine. Never silently replace a real provider with a mock."""
    from .core.config import AirvisConfig
    from .engine import AirvisEngine
    engine = AirvisEngine(AirvisConfig.load(search_from=None))
    if not [name for name in engine.providers.names() if name != "mock"]:
        try:
            from .core.asyncutil import run_blocking
            run_blocking(engine.close())
        except Exception: pass
        print("AIRVIS is not configured with a real provider.", file=sys.stderr)
        print("Run `airvis init` first.", file=sys.stderr)
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
                print("/status  show engine status\n/health  check providers/backends\n/voice   start voice mode\n/exit    quit"); continue
            if message == "/status": render(engine.describe(), "status"); continue
            if message == "/health":
                from .core.asyncutil import run_blocking
                render(run_blocking(engine.health_check()), "health"); continue
            if message == "/voice":
                try:
                    from .voice import run as run_voice
                    return run_voice(engine)
                except KeyboardInterrupt:
                    print("\n(interrupted)")
                    continue
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
    action = args[1] if len(args) > 1 else "list"
    registry = SkillRegistry()
    if action == "list": render(registry.list(), "skills"); return 0
    if action == "create" and len(args) >= 3: print(registry.create(args[2])); return 0
    if action in {"enable", "disable"} and len(args) >= 3:
        if not registry.enable(args[2], action == "enable"):
            print(f"skill not found: {args[2]}", file=sys.stderr); return 2
        print("updated"); return 0
    if action == "remove" and len(args) >= 3:
        if not registry.remove(args[2]):
            print(f"skill not found: {args[2]}", file=sys.stderr); return 2
        print("removed"); return 0
    print("usage: airvis skills [list|create|enable|disable|remove] [name]", file=sys.stderr); return 2


def _help() -> int:
    print("""AIRVIS — native AI orchestration CLI

Usage:
  airvis init                         first-run interactive setup
  airvis chat [message]              chat with the orchestrator
  airvis voice                       microphone → AIRVIS → ElevenLabs
  airvis list                        configured providers/channels
  airvis add <channel>               enable a channel in workspace config
  airvis remove <channel>            disable a channel
  airvis actions ...                 action/tool management
  airvis flow ...                    workflow management
  airvis status | health | doctor    diagnostics
  airvis config path                 show active config paths
  airvis setup [model|voice|...]     reconfigure a section
  airvis gateway                     start the AIRVIS gateway

Options:
  --json                             machine-readable output
  --version                         show version
""")
    return 0


def _config_command(args: list[str]) -> int:
    action = args[1] if len(args) > 1 else "show"
    if action == "path":
        print(f"setup:     {SETUP_PATH}")
        print(f"workspace: {WORKSPACE_CONFIG}")
        return 0
    if action == "show":
        if not SETUP_PATH.exists():
            print("AIRVIS is not initialized. Run `airvis init`.")
            return 2
        print(SETUP_PATH.read_text(encoding="utf-8"))
        return 0
    print("usage: airvis config [path|show]", file=sys.stderr)
    return 2


def _channel_command(action: str, channel: str) -> int:
    try:
        data = json.loads(WORKSPACE_CONFIG.read_text(encoding="utf-8")) if WORKSPACE_CONFIG.exists() else {}
    except json.JSONDecodeError:
        data = {}
    channels = data.get("channels")
    if isinstance(channels, dict):
        enabled = list(channels.get("enabled", []))
    elif isinstance(channels, list):
        enabled = list(channels)
    else:
        enabled = []
    if action == "add":
        if channel not in enabled: enabled.append(channel)
    else:
        enabled = [item for item in enabled if item != channel]
    data["channels"] = {"enabled": enabled}
    WORKSPACE_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ channel {action}ed: {channel}")
    if channel in {"telegram", "discord", "slack"}:
        print("  Run `airvis setup channels` to configure its credentials.")
    return 0


def _voice_command(args: list[str]) -> int:
    engine = _configured_engine()
    if engine is None: return 2
    try:
        from .voice import run as run_voice
        seconds = 6.0
        speak_answers = True
        if "--seconds" in args:
            index = args.index("--seconds")
            seconds = float(args[index + 1])
        if "--no-speak" in args:
            speak_answers = False
        return run_voice(engine, seconds=seconds, speak_answers=speak_answers)
    except KeyboardInterrupt:
        print("\n(interrupted)")
        return 130
    except Exception as exc:
        print(f"AIRVIS voice error: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            from .core.asyncutil import run_blocking
            run_blocking(engine.close())
        except Exception: pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--version"], ["-V"]):
        from . import __version__
        print(__version__); return 0
    if not args or args in (["--help"], ["-h"]):
        return _help() if args else _interactive_chat()
    if args[0] == "model": args = ["setup", "model", *args[1:]]
    elif args[0] == "init": args = ["setup", *args[1:]]
    elif args[0] == "tools" and len(args) == 1: args = ["tools", "list"]
    if args[0] == "setup":
        from .setup import run
        return run(args[1] if len(args) > 1 else None)
    if args[0] in {"skills", "skill"}: return _skills_command(args)
    if args[0] == "config": return _config_command(args)
    if args[0] == "add" and len(args) == 2: return _channel_command("add", args[1])
    if args[0] == "remove" and len(args) == 2: return _channel_command("remove", args[1])
    if args[0] == "list":
        if not SETUP_PATH.exists():
            print("AIRVIS is not initialized. Run `airvis init`."); return 2
        data = json.loads(SETUP_PATH.read_text(encoding="utf-8"))
        print("Providers:", ", ".join(data.get("providers", [])) or data.get("default_provider", "-"))
        print("Default model:", data.get("model", "-"))
        print("Channels:", ", ".join(data.get("channels", [])) or "-")
        print("Voice:", "enabled" if data.get("voice", {}).get("enabled") else "disabled")
        print("Orchestrator:", data.get("orchestrator", {}).get("strategy", "balanced"))
        return 0
    if args[0] == "voice": return _voice_command(args[1:])
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
