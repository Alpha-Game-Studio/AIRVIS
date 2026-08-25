"""Interactive first-run setup for AIRVIS.

AIRVIS is voice-first and orchestration-first. Setup configures the control
plane (providers/models, voice, channels and orchestration policy) rather than
forcing users into a text-only CLI workflow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SETUP_PATH = Path.home() / ".airvis" / "setup.json"
WORKSPACE_CONFIG = Path.cwd() / "airvis.json"
PLUGIN_DIR = Path.home() / ".airvis" / "plugins"
SKILL_DIR = Path.home() / ".airvis" / "skills"

PROVIDERS = ("openrouter", "openai", "anthropic", "gemini", "xai", "ollama", "mock")
MODELS: dict[str, tuple[str, ...]] = {
    "openrouter": ("openai/gpt-5-mini", "openai/gpt-5", "anthropic/claude-sonnet-4", "google/gemini-2.5-pro", "custom"),
    "openai": ("gpt-5-mini", "gpt-5", "gpt-4o-mini", "custom"),
    "anthropic": ("claude-sonnet-4", "claude-opus-4", "custom"),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-pro", "custom"),
    "xai": ("grok-4", "grok-3", "custom"),
    "ollama": ("llama3.2", "qwen3:8b", "qwen3:14b", "custom"),
    "mock": ("mock",),
}
CHANNELS = ("voice", "cli", "telegram", "discord", "slack", "web", "imessage")
STRATEGIES = ("balanced", "quality", "fast", "cheap", "premium", "local_only")
STT_PROVIDERS = ("openai", "speech_recognition", "none")
TTS_PROVIDERS = ("elevenlabs", "system", "none")


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, StopIteration):
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


def _list_input(title: str, defaults: list[str]) -> list[str]:
    value = _ask(title, ",".join(defaults))
    return [item.strip() for item in value.split(",") if item.strip()]


def run() -> int:
    existing = _load(SETUP_PATH)
    current = _load(WORKSPACE_CONFIG)

    old_provider = str(existing.get("default_provider") or existing.get("provider") or current.get("providers", {}).get("default") or "openrouter")
    providers = _multi("Providers (AIRVIS can route between these)", PROVIDERS, list(existing.get("providers", [old_provider]))) or [old_provider]
    default_provider = _choose("Default provider", tuple(providers), old_provider if old_provider in providers else providers[0])

    old_model = str(existing.get("model") or current.get("providers", {}).get("model") or "")
    model_options = MODELS.get(default_provider, ("custom",))
    model = _choose(f"Model for {default_provider}", model_options, old_model if old_model in model_options else model_options[0])
    if model == "custom":
        model = _ask(f"Enter {default_provider} model id", old_model or model_options[0])
    fallbacks = [item for item in providers if item != default_provider]

    channels = _multi("Channels", CHANNELS, list(existing.get("channels", ["voice"]))) or ["voice"]
    voice_enabled = "voice" in channels
    stt = _choose("Voice STT provider", STT_PROVIDERS, str(existing.get("voice", {}).get("stt_provider", "openai")))
    tts = _choose("Voice TTS provider", TTS_PROVIDERS, str(existing.get("voice", {}).get("tts_provider", "elevenlabs")))
    voice_id = _ask("TTS voice id (optional)", str(existing.get("voice", {}).get("voice_id", "")))
    language = _ask("Voice language", str(existing.get("voice", {}).get("language", "ko-KR")))

    strategy = _choose("Orchestration strategy", STRATEGIES, str(existing.get("orchestrator", {}).get("strategy", current.get("routing", {}).get("strategy", "balanced"))))
    max_concurrency = int(_ask("Maximum concurrent agent tasks", str(existing.get("orchestrator", {}).get("max_concurrency", 4))))
    review = _bool("Enable automatic review?", bool(existing.get("orchestrator", {}).get("review", True)))
    auto_repair = _bool("Enable automatic repair/retry?", bool(existing.get("orchestrator", {}).get("auto_repair", True)))
    plugins = _list_input("Enabled plugins", list(existing.get("plugins", [])))
    skills = _list_input("Enabled skills", list(existing.get("skills", [])))

    setup_data = {
        "version": 3,
        "runtime": "native",
        "provider": default_provider,
        "providers": providers,
        "default_provider": default_provider,
        "model": model,
        "fallback_providers": fallbacks,
        "channels": channels,
        "voice": {
            "enabled": voice_enabled,
            "stt_provider": stt,
            "tts_provider": tts,
            "voice_id": voice_id,
            "language": language,
        },
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
    current["providers"]["model"] = model
    current["providers"]["fallbacks"] = fallbacks
    current.setdefault("routing", {})["strategy"] = strategy
    current.setdefault("agents", {})["default_backend"] = "native"
    current["agents"]["default_max_concurrency"] = max_concurrency
    current.setdefault("backends", {})["enabled"] = ["native"]
    current["backends"]["enabled"] = ["native"]
    current["voice"] = {"enabled": voice_enabled, "stt_provider": stt, "tts_provider": tts, "voice_id": voice_id, "language": language}
    current["channels"] = {"enabled": channels}
    current.setdefault("workflow", {})["max_concurrency"] = max_concurrency
    current.setdefault("review", {})["enabled"] = review
    current.setdefault("repair", {})["allow_human_review"] = True
    if not auto_repair:
        current["repair"]["max_retries"] = 0
    WORKSPACE_CONFIG.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\nAIRVIS setup complete. Run `airvis gateway` for the voice-first gateway.")
    return 0


__all__ = ["run"]
