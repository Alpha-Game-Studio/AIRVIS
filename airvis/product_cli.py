"""AIRVIS product CLI.

A production command surface inspired by modern agent CLIs: setup/auth,
connections, action discovery/execution, durable flows, memory, model
selection, chat, research and optional voice. All execution uses AIRVIS's
real engine; there are no canned provider responses here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

VERSION = "8.2.0"
HOME = Path.home() / ".airvis"
SETUP = HOME / "setup.json"
CREDS = HOME / "credentials.env"


def load_setup() -> dict[str, Any]:
    try:
        value = json.loads(SETUP.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_setup(value: dict[str, Any]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    SETUP.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(value: Any, machine: bool = False) -> None:
    if machine or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print(value)


def engine(workspace: str | None = None, config: str | None = None):
    from .core.config import AirvisConfig
    from .engine import AirvisEngine
    return AirvisEngine(AirvisConfig.load(config, search_from=workspace), workspace=workspace)


def close(e: Any) -> None:
    from .core.asyncutil import run_blocking
    try:
        run_blocking(e.close())
    except Exception:
        pass


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="airvis", description="AIRVIS autonomous AI agent CLI")
    p.add_argument("--version", "-V", action="version", version=f"airvis {VERSION}")
    p.add_argument("--agent", "--json", action="store_true", dest="machine")
    p.add_argument("--workspace")
    p.add_argument("--config")
    s = p.add_subparsers(dest="command")

    init = s.add_parser("init", help="first-run setup")
    init.add_argument("--provider")
    init.add_argument("--model")
    init.add_argument("--api-key")
    s.add_parser("login", help="configure provider credentials")
    s.add_parser("logout", help="remove stored credentials")

    config = s.add_parser("config", help="inspect configuration")
    cs = config.add_subparsers(dest="config_command")
    cs.add_parser("path"); cs.add_parser("show"); cs.add_parser("reset")

    s.add_parser("list", help="list configured providers/channels")
    s.add_parser("platforms", help="list supported providers and channels")
    add = s.add_parser("add", help="enable a channel")
    add.add_argument("name"); add.add_argument("--tag")
    rm = s.add_parser("remove", help="disable a channel")
    rm.add_argument("name")

    model = s.add_parser("model", help="model selection")
    ms = model.add_subparsers(dest="model_command")
    ms.add_parser("list")
    select = ms.add_parser("select"); select.add_argument("provider"); select.add_argument("model")
    mcfg = ms.add_parser("config"); mcfg.add_argument("action", choices=["show", "set"]); mcfg.add_argument("provider", nargs="?"); mcfg.add_argument("model", nargs="?")

    actions = s.add_parser("actions", help="discover and execute AIRVIS tools")
    a = actions.add_subparsers(dest="actions_command")
    a.add_parser("list")
    search = a.add_parser("search"); search.add_argument("query")
    knowledge = a.add_parser("knowledge"); knowledge.add_argument("name")
    execute = a.add_parser("execute"); execute.add_argument("name"); execute.add_argument("connection", nargs="?"); execute.add_argument("-d", "--data", default="{}"); execute.add_argument("--confirm", action="store_true"); execute.add_argument("--dry-run", action="store_true")

    flow = s.add_parser("flow", help="reusable workflows")
    f = flow.add_subparsers(dest="flow_command")
    create = f.add_parser("create"); create.add_argument("key"); create.add_argument("--definition", required=True)
    validate = f.add_parser("validate"); validate.add_argument("key")
    run = f.add_parser("execute"); run.add_argument("key")
    f.add_parser("list")
    status = f.add_parser("status"); status.add_argument("workflow_id")
    resume = f.add_parser("resume"); resume.add_argument("workflow_id")

    mem = s.add_parser("mem", aliases=["memory"], help="persistent AIRVIS memory")
    m = mem.add_subparsers(dest="mem_command")
    addm = m.add_parser("add"); addm.add_argument("content")
    searchm = m.add_parser("search"); searchm.add_argument("query")
    m.add_parser("list")

    chat = s.add_parser("chat", help="chat with AIRVIS")
    chat.add_argument("message", nargs="*")
    research = s.add_parser("research", help="research/coding request")
    research.add_argument("request")
    voice = s.add_parser("voice", help="microphone -> STT -> AIRVIS -> ElevenLabs")
    voice.add_argument("--seconds", type=float, default=6.0); voice.add_argument("--no-speak", action="store_true")
    s.add_parser("status"); s.add_parser("health"); s.add_parser("doctor"); s.add_parser("guide"); s.add_parser("gateway")
    return p


def setup(args: argparse.Namespace) -> int:
    data = load_setup()
    if args.provider:
        data["default_provider"] = args.provider
        data.setdefault("providers", [])
        if args.provider not in data["providers"]: data["providers"].append(args.provider)
    if args.model: data["model"] = args.model
    if args.provider or args.model or args.api_key:
        save_setup(data)
        if args.api_key:
            HOME.mkdir(parents=True, exist_ok=True)
            key_name = (args.provider or data.get("default_provider") or "openrouter").upper() + "_API_KEY"
            CREDS.write_text(f"{key_name}={args.api_key}\n", encoding="utf-8")
        print("✓ AIRVIS configured")
        return 0
    from .setup import run
    return run(None)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    machine = args.machine
    if args.command is None: return guide()
    if args.command == "init": return setup(args)
    if args.command == "login":
        from .setup import run
        return run("model")
    if args.command == "logout":
        CREDS.unlink(missing_ok=True); print("✓ credentials removed"); return 0
    if args.command == "config": return config_command(args, machine)
    if args.command == "list": return list_command(machine)
    if args.command == "platforms":
        emit({"providers": ["openrouter", "openai", "anthropic", "gemini", "xai", "ollama"], "channels": ["cli", "voice", "telegram", "discord", "slack", "web", "imessage"], "voice": {"stt": ["openai", "system"], "tts": ["elevenlabs", "system"]}}, machine); return 0
    if args.command in {"add", "remove"}: return channel_command(args, args.command == "add")
    if args.command == "model": return model_command(args, machine)
    if args.command == "actions": return actions_command(args, machine)
    if args.command == "flow": return flow_command(args, machine)
    if args.command in {"mem", "memory"}: return memory_command(args, machine)
    if args.command == "chat":
        message = " ".join(args.message).strip()
        return chat(message, args.workspace, args.config) if message else interactive(args.workspace, args.config)
    if args.command == "research": return chat(args.request, args.workspace, args.config)
    if args.command == "voice": return voice_command(args)
    if args.command == "gateway":
        from .launcher import main as launcher
        return launcher(["gateway"])
    if args.command in {"status", "health", "doctor"}:
        from .cli import main as cli
        forwarded = [args.command]
        if args.workspace: forwarded = ["--workspace", args.workspace, *forwarded]
        if args.config: forwarded = ["--config", args.config, *forwarded]
        if machine: forwarded.insert(0, "--json")
        return cli(forwarded)
    if args.command == "guide": return guide()
    return 2


def config_command(args: argparse.Namespace, machine: bool) -> int:
    if args.config_command == "path":
        emit({"setup": str(SETUP), "credentials": str(CREDS), "workspace": str(Path.cwd() / "airvis.json")}, machine); return 0
    if args.config_command == "reset":
        SETUP.unlink(missing_ok=True); CREDS.unlink(missing_ok=True); print("✓ configuration reset"); return 0
    data = load_setup()
    if not data: emit({"initialized": False}, machine); return 2
    emit(data, machine); return 0


def list_command(machine: bool) -> int:
    data = load_setup()
    emit({"providers": data.get("providers", []), "default_provider": data.get("default_provider"), "model": data.get("model"), "channels": data.get("channels", []), "voice": data.get("voice", {})}, machine); return 0


def channel_command(args: argparse.Namespace, add: bool) -> int:
    data = load_setup(); channels = data.get("channels", [])
    if isinstance(channels, dict): channels = channels.get("enabled", [])
    channels = list(channels) if isinstance(channels, list) else []
    if add and args.name not in channels: channels.append(args.name)
    if not add: channels = [x for x in channels if x != args.name]
    data["channels"] = channels
    if getattr(args, "tag", None): data.setdefault("channel_tags", {})[args.name] = args.tag
    save_setup(data); print(f"✓ {'added' if add else 'removed'} {args.name}"); return 0


def model_command(args: argparse.Namespace, machine: bool) -> int:
    data = load_setup()
    if args.model_command in {None, "list"}: emit({"provider": data.get("default_provider"), "model": data.get("model"), "providers": data.get("providers", [])}, machine); return 0
    if args.model_command == "config" and args.action == "show": emit({"provider": data.get("default_provider"), "model": data.get("model")}, machine); return 0
    provider, model = args.provider, args.model
    if not provider or not model: print("usage: airvis model select <provider> <model>", file=sys.stderr); return 2
    data["default_provider"], data["model"] = provider, model; data.setdefault("providers", [])
    if provider not in data["providers"]: data["providers"].append(provider)
    save_setup(data); print(f"✓ selected {provider}/{model}"); return 0


def actions_command(args: argparse.Namespace, machine: bool) -> int:
    e = engine(args.workspace, args.config)
    try:
        tools = e.tools; items = tools.list()
        if args.actions_command in {None, "list"}: emit(items, machine); return 0
        if args.actions_command == "search":
            q = args.query.lower(); emit([x for x in items if q in str(x).lower()], machine); return 0
        if args.actions_command == "knowledge": emit(tools.get(args.name).schema(), machine); return 0
        raw = json.loads(args.data or "{}")
        if args.dry_run: emit({"dry_run": True, "tool": args.name, "arguments": raw}, machine); return 0
        from .core.asyncutil import run_blocking
        from .security.permissions import always_approve
        result = run_blocking(tools.call(args.name, raw, confirm=args.confirm, approval_handler=always_approve if args.confirm else None))
        emit(result.to_dict(), machine); return 0 if result.ok else 1
    finally: close(e)


def flow_command(args: argparse.Namespace, machine: bool) -> int:
    root = Path.cwd() / ".airvis" / "flows" / args.key if getattr(args, "key", None) else None
    if args.flow_command == "create":
        try: definition = json.loads(args.definition)
        except ValueError as exc: print(f"invalid flow JSON: {exc}", file=sys.stderr); return 2
        root.mkdir(parents=True, exist_ok=True); (root / "flow.json").write_text(json.dumps(definition, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); print(f"✓ flow created: {args.key}"); return 0
    if args.flow_command == "validate":
        path = root / "flow.json"
        if not path.is_file(): print(f"flow not found: {args.key}", file=sys.stderr); return 2
        try: json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc: print(f"✗ invalid flow: {exc}", file=sys.stderr); return 1
        print(f"✓ flow valid: {args.key}"); return 0
    e = engine()
    try:
        from .core.asyncutil import run_blocking
        if args.flow_command == "list": emit(e.store.list_workflows(), machine); return 0
        if args.flow_command == "status": emit(e.orchestrator.status(args.workflow_id), machine); return 0
        if args.flow_command == "resume":
            result = run_blocking(e.resume(args.workflow_id)); emit(result.to_dict(), machine); return 0 if result.ok else 1
        if args.flow_command == "execute":
            definition = json.loads((root / "flow.json").read_text(encoding="utf-8")); request = definition.get("request") or definition.get("prompt") or definition.get("description") or f"Execute flow {args.key}"
            result = run_blocking(e.run(str(request))); emit(result.to_dict(), machine); return 0 if result.ok else 1
    finally: close(e)
    return 2


def memory_command(args: argparse.Namespace, machine: bool) -> int:
    e = engine(args.workspace, args.config)
    try:
        if args.mem_command == "add": emit({"id": e.memory.add(args.content)}, machine); return 0
        if args.mem_command == "search":
            q = args.query.lower(); emit([x for x in e.memory.list() if q in str(x.get("content", "")).lower()], machine); return 0
        emit(e.memory.list(), machine); return 0
    finally: close(e)


def chat(message: str, workspace: str | None, config: str | None) -> int:
    e = engine(workspace, config)
    try:
        real = [x for x in e.providers.names() if x != "mock"]
        if not real: print("No real AI provider configured. Run `airvis init`.", file=sys.stderr); return 2
        result = e.run_sync(message); print(str(getattr(result, "output", result)).strip()); return 0 if getattr(result, "ok", True) else 1
    finally: close(e)


def voice_command(args: argparse.Namespace) -> int:
    e = engine(args.workspace, args.config)
    try:
        from .voice import run
        return run(e, seconds=args.seconds, speak_answers=not args.no_speak)
    except Exception as exc: print(f"AIRVIS voice error: {exc}", file=sys.stderr); return 1
    finally: close(e)


def interactive(workspace: str | None, config: str | None) -> int:
    e = engine(workspace, config)
    try:
        print("AIRVIS — native agent"); print("/help  /status  /voice  /exit")
        while True:
            try: line = input("you › ").strip()
            except (EOFError, KeyboardInterrupt): print(); return 0
            if not line: continue
            if line in {"/exit", "/quit"}: return 0
            if line == "/help": guide(); continue
            if line == "/status": emit(e.describe()); continue
            if line == "/voice":
                from .voice import run
                run(e); continue
            result = e.run_sync(line); print(f"\nairvis › {str(getattr(result, 'output', result)).strip()}\n")
    finally: close(e)


def guide() -> int:
    print("""AIRVIS — autonomous AI agent CLI

Setup:       init, login, logout, config, model
Connections: add, remove, list, platforms
Actions:     actions list|search|knowledge|execute
Flows:       flow create|validate|execute|list|status|resume
Memory:      mem add|search|list
Agent:       chat, research, voice
Diagnostics: status, health, doctor, gateway

Use --agent for machine-readable JSON output.
""")
    return 0


if __name__ == "__main__": raise SystemExit(main())
