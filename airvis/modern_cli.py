"""Product CLI for AIRVIS.

The UX follows modern agent CLIs: first-run init, discovery commands,
interactive prompting, slash commands, JSON/agent mode, and explicit
model/voice configuration. Execution remains owned by the AIRVIS native engine.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

VERSION = "8.2"
SETUP_PATH = Path.home() / ".airvis" / "setup.json"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _load_setup() -> dict[str, Any]:
    if not SETUP_PATH.is_file():
        return {}
    try:
        value = json.loads(SETUP_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _engine(workspace: str | None = None, config_path: str | None = None):
    from .core.config import AirvisConfig
    from .engine import AirvisEngine
    config = AirvisConfig.load(config_path, search_from=workspace)
    return AirvisEngine(config, workspace=workspace)


def _emit(value: Any, agent: bool) -> None:
    print(_json(value) if agent or isinstance(value, (dict, list)) else value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="airvis", description="AIRVIS native AI agent CLI")
    parser.add_argument("--version", "-V", action="version", version="%(prog)s " + VERSION)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--agent", action="store_true", help="machine-readable JSON mode")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON mode")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="first-run setup")
    sub.add_parser("setup", help="reconfigure AIRVIS")
    sub.add_parser("login", help="configure the model provider")
    sub.add_parser("logout", help="remove local AIRVIS credentials")
    sub.add_parser("status")
    sub.add_parser("health")
    sub.add_parser("doctor")
    sub.add_parser("list", help="show configured providers, channels and voice")
    sub.add_parser("platforms", help="show supported providers and channels")
    sub.add_parser("guide", help="show CLI usage")

    model = sub.add_parser("model", help="configure and inspect models")
    model_sub = model.add_subparsers(dest="model_command")
    model_sub.add_parser("list")
    mc = model_sub.add_parser("config")
    mc.add_argument("action", choices=("set", "show"))
    mc.add_argument("provider", nargs="?")
    mc.add_argument("model", nargs="?")
    ms = model_sub.add_parser("select")
    ms.add_argument("provider")
    ms.add_argument("model")

    actions = sub.add_parser("actions", help="discover and execute AIRVIS tools")
    actions_sub = actions.add_subparsers(dest="actions_command")
    actions_sub.add_parser("list")
    search = actions_sub.add_parser("search")
    search.add_argument("query")
    show = actions_sub.add_parser("knowledge")
    show.add_argument("name")
    execute = actions_sub.add_parser("execute")
    execute.add_argument("name")
    execute.add_argument("arguments", nargs="?", default="{}")
    execute.add_argument("--confirm", action="store_true")

    flow = sub.add_parser("flow", help="run and inspect durable AIRVIS workflows")
    flow_sub = flow.add_subparsers(dest="flow_command")
    fr = flow_sub.add_parser("run")
    fr.add_argument("request")
    fs = flow_sub.add_parser("status")
    fs.add_argument("workflow_id")
    flow_sub.add_parser("list")

    mem = sub.add_parser("mem", aliases=["memory"], help="AIRVIS local memory")
    mem_sub = mem.add_subparsers(dest="mem_command")
    ma = mem_sub.add_parser("add")
    ma.add_argument("content")
    msrch = mem_sub.add_parser("search")
    msrch.add_argument("query")
    mem_sub.add_parser("list")

    chat = sub.add_parser("chat", help="single request or interactive chat")
    chat.add_argument("message", nargs="*")
    voice = sub.add_parser("voice", help="microphone -> STT -> AIRVIS -> ElevenLabs")
    voice.add_argument("--seconds", type=float, default=6.0)
    voice.add_argument("--no-speak", action="store_true")

    research = sub.add_parser("research", help="run a request through the native agent")
    research.add_argument("request")
    return parser


def _setup(section: str | None = None) -> int:
    from .setup import run
    return run(section)


def _list_config(agent: bool) -> int:
    data = _load_setup()
    if not data:
        _emit({"initialized": False, "hint": "Run `airvis init`"}, agent)
        return 2
    _emit({
        "initialized": True,
        "provider": data.get("default_provider", data.get("provider")),
        "model": data.get("model"),
        "providers": data.get("providers", []),
        "channels": data.get("channels", []),
        "voice": data.get("voice", {}),
        "orchestrator": data.get("orchestrator", {}),
        "plugins": data.get("plugins", []),
        "skills": data.get("skills", []),
    }, agent)
    return 0


def _platforms(agent: bool) -> int:
    _emit({
        "providers": ["openrouter", "openai", "anthropic", "gemini", "xai", "ollama"],
        "channels": ["cli", "voice", "telegram", "discord", "slack", "web", "imessage"],
        "voice": {"stt": ["openai", "system", "none"], "tts": ["elevenlabs", "system", "none"]},
    }, agent)
    return 0


def _guide() -> int:
    print("""AIRVIS — native AI agent CLI

FIRST RUN
  airvis init                         configure provider, model, voice and channels
  airvis setup                        reconfigure AIRVIS
  airvis login / logout               manage local provider credentials

DISCOVERY
  airvis list                         current configuration
  airvis platforms                    supported providers/channels
  airvis status | health | doctor     diagnostics
  airvis model list                   configured model
  airvis actions list                 native tools
  airvis actions search <query>       find tools by name/description
  airvis actions knowledge <tool>     inspect a tool schema

WORK
  airvis                             interactive agent shell
  airvis chat <request>               one-shot request
  airvis research <request>           one-shot research/development request
  airvis flow run <request>           durable workflow
  airvis mem add/search/list          local memory

VOICE
  airvis voice                        microphone -> STT -> AIRVIS -> ElevenLabs
  In the interactive shell: /voice

AGENT MODE
  airvis --agent status
  airvis --agent actions list
  airvis --agent chat "inspect this project"
""")
    return 0


def _model(args: argparse.Namespace, agent: bool) -> int:
    data = _load_setup()
    if args.model_command in (None, "list"):
        _emit({"provider": data.get("default_provider"), "model": data.get("model"), "providers": data.get("providers", [])}, agent)
        return 0
    if args.model_command == "config" and args.action == "show":
        _emit({"provider": data.get("default_provider"), "model": data.get("model")}, agent)
        return 0
    if args.model_command == "config" and args.action == "set":
        if not args.provider or not args.model:
            print("usage: airvis model config set <provider> <model>", file=sys.stderr)
            return 2
        from .setup import _save_model, _env_file, _load, WORKSPACE_CONFIG
        current = _load(WORKSPACE_CONFIG)
        existing = _load(SETUP_PATH)
        _save_model(args.provider, args.model, _env_file(), current, existing)
        return 0
    if args.model_command == "select":
        from .setup import _save_model, _env_file, _load, WORKSPACE_CONFIG
        current = _load(WORKSPACE_CONFIG)
        existing = _load(SETUP_PATH)
        _save_model(args.provider, args.model, _env_file(), current, existing)
        return 0
    return 2


def _actions(args: argparse.Namespace, agent: bool, workspace: str | None, config_path: str | None) -> int:
    engine = _engine(workspace, config_path)
    try:
        tools = engine.tools
        if args.actions_command in (None, "list"):
            _emit(tools.list(), agent)
            return 0
        if args.actions_command == "search":
            query = args.query.lower()
            items = [item for item in tools.list() if query in str(item).lower()]
            _emit(items, agent)
            return 0
        if args.actions_command == "knowledge":
            _emit(tools.get(args.name).schema(), agent)
            return 0
        if args.actions_command == "execute":
            try:
                arguments = json.loads(args.arguments)
            except ValueError as exc:
                print(f"invalid JSON arguments: {exc}", file=sys.stderr)
                return 2
            from .core.asyncutil import run_blocking
            from .security.permissions import always_approve
            result = run_blocking(tools.call(args.name, arguments, confirm=args.confirm, approval_handler=always_approve if args.confirm else None))
            _emit(result.to_dict(), agent)
            return 0 if result.ok else 1
        return 2
    finally:
        from .core.asyncutil import run_blocking
        run_blocking(engine.close())


def _flow(args: argparse.Namespace, agent: bool, workspace: str | None, config_path: str | None) -> int:
    engine = _engine(workspace, config_path)
    try:
        from .core.asyncutil import run_blocking
        if args.flow_command == "run":
            result = run_blocking(engine.run(args.request))
            _emit(result.to_dict(), agent)
            return 0 if result.ok else 1
        if args.flow_command == "status":
            _emit(engine.orchestrator.status(args.workflow_id), agent)
            return 0
        _emit(engine.store.list_workflows(), agent)
        return 0
    finally:
        run_blocking(engine.close())


def _memory(args: argparse.Namespace, agent: bool, workspace: str | None, config_path: str | None) -> int:
    engine = _engine(workspace, config_path)
    try:
        if args.mem_command == "add":
            _emit({"id": engine.memory.add(args.content)}, agent)
        elif args.mem_command == "search":
            query = args.query.lower()
            matches = [item for item in engine.memory.list() if query in str(item.get("content", "")).lower()]
            _emit(matches, agent)
        else:
            _emit(engine.memory.list(), agent)
        return 0
    finally:
        from .core.asyncutil import run_blocking
        run_blocking(engine.close())


def _chat(message: str, workspace: str | None, config_path: str | None, voice: bool = False, seconds: float = 6.0, speak: bool = True) -> int:
    engine = _engine(workspace, config_path)
    try:
        if voice:
            from .voice import run
            return run(engine, seconds=seconds, speak_answers=speak)
        from .core.asyncutil import run_blocking
        result = run_blocking(engine.run(message))
        print(str(result.output).strip())
        return 0 if result.ok else 1
    finally:
        from .core.asyncutil import run_blocking
        run_blocking(engine.close())


def interactive(workspace: str | None, config_path: str | None) -> int:
    print("AIRVIS — native AI agent")
    print("Type /help for commands, /voice for voice mode, /exit to quit.\n")
    while True:
        try:
            line = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return 0
        if line == "/help":
            _guide(); continue
        if line == "/status":
            from .launcher import main
            main(["--config", config_path, "status"] if config_path else ["status"]); continue
        if line == "/voice":
            try:
                _chat("", workspace, config_path, voice=True)
            except KeyboardInterrupt:
                print("\n(interrupted)")
            continue
        if line == "/models":
            _model(argparse.Namespace(model_command="list"), False); continue
        try:
            _chat(line, workspace, config_path)
        except KeyboardInterrupt:
            print("\n(interrupted)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    agent = bool(args.agent or args.json)
    if not args.command:
        return interactive(args.workspace, args.config)
    if args.command in {"init", "setup"}:
        return _setup(None if args.command == "init" else "full")
    if args.command == "login":
        return _setup("model")
    if args.command == "logout":
        path = Path.home() / ".airvis" / "credentials.env"
        if path.exists(): path.unlink()
        print("AIRVIS credentials removed")
        return 0
    if args.command == "guide":
        return _guide()
    if args.command == "platforms":
        return _platforms(agent)
    if args.command == "list":
        return _list_config(agent)
    if args.command == "model":
        return _model(args, agent)
    if args.command == "actions":
        return _actions(args, agent, args.workspace, args.config)
    if args.command == "flow":
        return _flow(args, agent, args.workspace, args.config)
    if args.command in {"mem", "memory"}:
        return _memory(args, agent, args.workspace, args.config)
    if args.command == "voice":
        return _chat("", args.workspace, args.config, voice=True, seconds=args.seconds, speak=not args.no_speak)
    if args.command in {"chat", "research"}:
        message = " ".join(args.message if args.command == "chat" else [args.request]).strip()
        if not message:
            return interactive(args.workspace, args.config)
        return _chat(message, args.workspace, args.config)
    if args.command in {"status", "health", "doctor"}:
        from .launcher import main
        forwarded = [args.command]
        if args.workspace: forwarded = ["--workspace", args.workspace, *forwarded]
        if args.config: forwarded = ["--config", args.config, *forwarded]
        return main(forwarded)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
