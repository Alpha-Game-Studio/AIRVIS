"""Modern AIRVIS command surface."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import __version__

SETUP_PATH = Path.home() / ".airvis" / "setup.json"


def _load_setup() -> dict[str, Any] | None:
    if not SETUP_PATH.is_file():
        return None
    try:
        data = json.loads(SETUP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _platforms() -> int:
    print(json.dumps({
        "providers": ["openrouter", "openai", "anthropic", "gemini", "xai", "ollama"],
        "channels": ["voice", "terminal"],
        "voice": {"stt": ["openai"], "tts": ["elevenlabs"]},
    }, ensure_ascii=False))
    return 0


def _guide() -> int:
    print("""AIRVIS modern guide

First run:
  airvis init

Inspect and operate:
  airvis actions
  airvis agent list
  airvis agent model list
  airvis voice

The modern CLI is backed by the same native AIRVIS engine.
""")
    return 0


def _agent(args: list[str]) -> int:
    if not args:
        print("usage: airvis --agent <platforms|list|model>")
        return 2
    action = args[0]
    setup = _load_setup()
    if action == "platforms":
        return _platforms()
    if action == "list":
        if setup is None:
            print(json.dumps({"initialized": False}, ensure_ascii=False))
            return 2
        agents = setup.get("agents", [])
        if not isinstance(agents, list):
            agents = []
        print(json.dumps({"initialized": True, "agents": agents}, ensure_ascii=False))
        return 0
    if action == "model":
        if len(args) < 2 or args[1] != "list":
            print("usage: airvis --agent model list")
            return 2
        if setup is None:
            print(json.dumps({"initialized": False}, ensure_ascii=False))
            return 2
        print(json.dumps({
            "provider": setup.get("default_provider", setup.get("provider", "")),
            "model": setup.get("model", ""),
        }, ensure_ascii=False))
        return 0
    print(f"unknown agent action: {action}")
    return 2


def _actions() -> int:
    from .launcher import _help
    return _help()


def main(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    if args in (["--version"], ["-V"]):
        print(f"airvis {__version__.rsplit('.', 1)[0]}")
        return 0
    if args in (["guide"], ["--guide"]):
        return _guide()
    if args and args[0] == "--agent":
        return _agent(args[1:])
    if args and args[0] == "actions":
        return _actions()
    if not args or args == ["--help"]:
        print("airvis 8.2 — modern AIRVIS CLI")
        print("Use: airvis init | airvis actions | airvis voice | airvis --agent platforms")
        return 0
    print(f"unknown command: {' '.join(args)}")
    return 2


__all__ = ["SETUP_PATH", "main"]
