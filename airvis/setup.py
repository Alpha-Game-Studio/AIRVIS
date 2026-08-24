"""Interactive first-run setup for AIRVIS."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".airvis" / "config.json"
PROVIDERS = ("openai", "anthropic", "gemini", "xai", "openrouter", "ollama", "mock")
CHANNELS = ("cli", "telegram", "discord", "slack", "web", "imessage")
STRATEGIES = ("balanced", "cheap", "fast", "quality", "premium", "local_only")
BACKENDS = ("native", "openclaw", "hermes")


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def _choose(title: str, options: tuple[str, ...], default: str) -> str:
    if default not in options:
        default = options[0]
    print(f"\n{title}")
    for index, option in enumerate(options, 1):
        print(f" {'*' if option == default else ' '} {index}. {option}")
    while True:
        raw = _ask("Select", str(options.index(default) + 1))
        try:
            index = int(raw) - 1
            if 0 <= index < len(options):
                return options[index]
        except ValueError:
            if raw in options:
                return raw
        print("Please select a valid option.")


def _multi(title: str, options: tuple[str, ...], defaults: list[str]) -> list[str]:
    defaults = [item for item in defaults if item in options]
    print(f"\n{title}")
    for index, option in enumerate(options, 1):
        print(f" {'*' if option in defaults else ' '} {index}. {option}")
    raw = _ask("Select comma-separated numbers", ",".join(str(options.index(x) + 1) for x in defaults))
    selected: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        try:
            index = int(item) - 1
            if 0 <= index < len(options) and options[index] not in selected:
                selected.append(options[index])
        except ValueError:
            if item in options and item not in selected:
                selected.append(item)
    return selected


def run() -> int:
    print("\n╭──────────────────────────────────────╮")
    print("│          AIRVIS 8.2 SETUP            │")
    print("╰──────────────────────────────────────╯")
    print("Configure AIRVIS once. Run `airvis setup` again whenever you want.\n")

    existing: dict[str, Any] = {}
    if CONFIG_PATH.is_file():
        try:
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    provider = _choose("Provider", PROVIDERS, str(existing.get("provider", "ollama")))
    orchestrator = existing.get("orchestrator", {})
    strategy = _choose("Orchestrator strategy", STRATEGIES, str(orchestrator.get("strategy", "balanced")))
    backend = _choose("Agent runtime", BACKENDS, str(orchestrator.get("backend", "native")))
    channels = _multi("Channels", CHANNELS, list(existing.get("channels", ["cli"]))) or ["cli"]

    print("\nPlugins and skills are local metadata and can be expanded by future plugin packs.")
    plugins = _ask("Enabled plugins (comma-separated)", ",".join(existing.get("plugins", [])))
    skills = _ask("Enabled skills (comma-separated)", ",".join(existing.get("skills", [])))

    if provider not in {"ollama", "mock"}:
        print("\nAPI keys are never written to the AIRVIS config file.")
        print("Set the provider's environment variable instead (for example OPENAI_API_KEY).")
        if _ask("API key available in your environment?", "y").lower() not in {"y", "yes"}:
            print("Note: configure the provider key before making a real request.")

    data = {
        "provider": provider,
        "channels": channels,
        "orchestrator": {"strategy": strategy, "backend": backend, "review": True, "auto_repair": True},
        "plugins": [item.strip() for item in plugins.split(",") if item.strip()],
        "skills": [item.strip() for item in skills.split(",") if item.strip()],
        "providers": {"default": provider, "fallbacks": []},
        "routing": {"strategy": strategy},
        "backends": {"enabled": [backend]},
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n✓ Configuration saved to {CONFIG_PATH}")
    print(f"✓ Provider: {provider}")
    print(f"✓ Orchestrator: {strategy} / {backend}")
    print(f"✓ Channels: {', '.join(channels)}")
    print("\nSetup complete. Run `airvis status` to inspect the engine.")
    return 0
