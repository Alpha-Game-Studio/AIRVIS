"""Interactive first-run setup for AIRVIS.

AIRVIS is a native agent operating system. External runtimes such as OpenClaw
or Hermes are not required to run the engine and are deliberately not selected
by this wizard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SETUP_PATH = Path.home() / ".airvis" / "setup.json"
WORKSPACE_CONFIG = Path.cwd() / "airvis.json"
PLUGIN_DIR = Path.home() / ".airvis" / "plugins"
SKILL_DIR = Path.home() / ".airvis" / "skills"

PROVIDERS = ("ollama", "openai", "anthropic", "gemini", "xai", "openrouter", "mock")
CHANNELS = ("cli", "telegram", "discord", "slack", "web", "imessage")
STRATEGIES = ("balanced", "cheap", "fast", "quality", "premium", "local_only")


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, StopIteration):
        # Setup is also used by tests, installers and piped/non-interactive
        # environments. Exhausted input must mean "accept the default", not
        # crash halfway through the wizard.
        return default
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
    default_text = ",".join(str(options.index(x) + 1) for x in defaults) or "1"
    raw = _ask("Select comma-separated numbers", default_text)
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


def _list_input(title: str, defaults: list[str]) -> list[str]:
    value = _ask(title, ",".join(defaults))
    return [item.strip() for item in value.split(",") if item.strip()]


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _bool(prompt: str, default: bool) -> bool:
    raw = _ask(prompt, "y" if default else "n").lower()
    return raw in {"y", "yes", "1", "true", "on"}


def run() -> int:
    print("\n╭────────────────────────────────────────────╮")
    print("│              AIRVIS 8.2 SETUP              │")
    print("│       Native Agent Operating System        │")
    print("╰────────────────────────────────────────────╯")
    print("Configure AIRVIS like an agent OS. Run `airvis setup` again to edit it.\n")
    print("Runtime: AIRVIS Native Engine (always enabled)")
    print("OpenClaw/Hermes are NOT required and are not selected by this setup.\n")

    existing = _load(SETUP_PATH)
    current = _load(WORKSPACE_CONFIG)

    previous_providers = list(existing.get("providers", []))
    if not previous_providers and existing.get("provider"):
        previous_providers = [str(existing["provider"])]
    if not previous_providers:
        previous_providers = [str(current.get("providers", {}).get("default", "ollama"))]
    providers = _multi("Providers", PROVIDERS, previous_providers) or ["ollama"]
    default_provider = _choose("Default provider", tuple(providers), providers[0])
    fallbacks = [item for item in providers if item != default_provider]

    channels = _multi("Channels", CHANNELS, list(existing.get("channels", ["cli"]))) or ["cli"]

    old_orchestrator = existing.get("orchestrator", {})
    strategy = _choose(
        "Orchestrator strategy", STRATEGIES,
        str(old_orchestrator.get("strategy", current.get("routing", {}).get("strategy", "balanced"))),
    )
    max_concurrency = int(_ask("Maximum concurrent agent tasks", str(old_orchestrator.get("max_concurrency", 4))))
    review = _bool("Enable automatic review?", bool(old_orchestrator.get("review", True)))
    auto_repair = _bool("Enable automatic repair/retry?", bool(old_orchestrator.get("auto_repair", True)))

    print("\nPlugins")
    print("Plugins are native AIRVIS extensions. Enter installed plugin IDs separated by commas.")
    plugins = _list_input("Enabled plugins", list(existing.get("plugins", [])))

    print("\nSkills")
    print("Skills are reusable instructions/capability packs loaded by the native agent.")
    skills = _list_input("Enabled skills", list(existing.get("skills", [])))

    if any(item not in {"ollama", "mock"} for item in providers):
        print("\nProvider credentials")
        print("API keys are never written to AIRVIS config. Use the provider's environment variable.")
        print("Examples: OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, XAI_API_KEY.")

    setup_data = {
        "version": 2,
        "runtime": "native",
        "providers": providers,
        "default_provider": default_provider,
        "fallback_providers": fallbacks,
        "channels": channels,
        "orchestrator": {
            "strategy": strategy,
            "backend": "native",
            "max_concurrency": max_concurrency,
            "review": review,
            "auto_repair": auto_repair,
        },
        "plugins": plugins,
        "skills": skills,
    }

    SETUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    SETUP_PATH.write_text(json.dumps(setup_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    current.setdefault("providers", {})["default"] = default_provider
    current["providers"]["fallbacks"] = fallbacks
    current.setdefault("routing", {})["strategy"] = strategy
    current.setdefault("agents", {})["default_backend"] = "native"
    current["agents"]["default_max_concurrency"] = max_concurrency
    current.setdefault("backends", {})["enabled"] = ["native"]
    current.setdefault("review", {})["enabled"] = review
    current.setdefault("repair", {})["allow_human_review"] = True
    if not auto_repair:
        current["repair"]["max_retries"] = 0

    WORKSPACE_CONFIG.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n✓ Setup metadata: {SETUP_PATH}")
    print(f"✓ Engine config: {WORKSPACE_CONFIG}")
    print("✓ Runtime: AIRVIS Native Engine")
    print(f"✓ Providers: {', '.join(providers)} (default: {default_provider})")
    print(f"✓ Channels: {', '.join(channels)}")
    print(f"✓ Orchestrator: {strategy}, concurrency={max_concurrency}")
    print(f"✓ Plugins: {', '.join(plugins) if plugins else 'none'}")
    print(f"✓ Skills: {', '.join(skills) if skills else 'none'}")
    print("\nSetup complete. Run `airvis status`, `airvis health`, or `airvis chat \"...\"`.")
    return 0


__all__ = ["run"]
