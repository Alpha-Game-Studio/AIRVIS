"""Interactive first-run setup for AIRVIS.

The wizard intentionally stores human-facing configuration separately from the
execution engine. The core config consumes provider/routing/backend settings,
while channel/plugin/skill selections are persisted as AIRVIS metadata for the
next setup and future runtime integrations.
"""

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
    print(f"\n{title}")
    for index, option in enumerate(options, 1):
        marker = "*" if option == default else " "
        print(f" {marker} {index}. {option}")
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
    print(f"\n{title}")
    for index, option in enumerate(options, 1):
        marker = "*" if option in defaults else " "
        print(f" {marker} {index}. {option}")
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
    print("Configure AIRVIS once. You can run `airvis setup` again at any time.\n")

    existing: dict[str, Any] = {}
    if CONFIG_PATH.is_file():
        try:
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}

    provider = _choose("Provider", PROVIDERS, str(existing.get("provider", "ollama")))
    strategy = _choose("Orchestrator strategy", STRATEGIES, str(existing.get("orchestrator", {}).get("strategy", "balanced")))
    backend = _choose("Agent runtime", BACKENDS, str(existing.get("orchestrator", {}).get("backend", "native")))
    channels = _multi("Channels", CHANNELS, list(existing.get("channels", ["cli"])))

    print("\nPlugins and skills are managed as local metadata for now.")
    plugins = _ask("Enabled plugins (comma-separated)", ",".join(existing.get("plugins", [])))
    skills = _ask("Enabled skills (comma-separated)", ",".join(existing.get("skills", [])))

    api_key = ""
    if provider not in {"ollama", "mock"}:
        print("\nAPI keys are not written to the AIRVIS config file.")
        print("Set the provider's environment variable instead (for example OPENAI_API_KEY).")
        print("This keeps secrets out of source control and config backups.")
        api_key = _ask("Provider API key (optional; set manually in your shell)")
        if api_key:
            print("API key received but intentionally not persisted.")

    data = {
        "provider": provider,
        "channels": channels or ["cli"],
        "orchestrator": {
            "strategy": strategy,
            "backend": backend,
            "review": True,
            "auto_repair": True,
        },
        "plugins": [item.strip() for item in plugins.split(",") if item.strip()],
        "skills": [item.strip() for item in skills.split(",") if item.strip()],
        "providers": {"default": provider, "fallbacks": [] if provider == "mock" else ["mock"]},
        "routing": {"strategy": strategy},
        "backends": {"enabled": [backend]},
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n✓ Configuration saved to {CONFIG_PATH}")
    print(f"✓ Provider: {provider}")
    print(f"✓ Orchestrator: {strategy} / {backend}")
    print(f"✓ Channels: {', '.join(data['channels'])}")
    print("\nSetup complete. Run `airvis status` to inspect the engine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
