"""Interactive first-run setup for AIRVIS.

Setup configures real runtime dependencies: provider credentials, model,
voice, channels and orchestration. Secrets are stored in a user-only env file
and are loaded by the provider/channel layer; they are never written to the
workspace configuration.
"""

from __future__ import annotations

import getpass
import json
import os
import stat
import sys
import termios
import tty
from pathlib import Path
from typing import Any

SETUP_PATH = Path.home() / ".airvis" / "setup.json"
CREDENTIALS_PATH = Path.home() / ".airvis" / "credentials.env"
WORKSPACE_CONFIG = Path.cwd() / "airvis.json"
PLUGIN_DIR = Path.home() / ".airvis" / "plugins"
SKILL_DIR = Path.home() / ".airvis" / "skills"

PROVIDERS = ("openrouter", "openai", "anthropic", "gemini", "xai", "ollama")
MODELS: dict[str, tuple[str, ...]] = {
    "openrouter": (
        "openai/gpt-5-mini", "openai/gpt-5", "anthropic/claude-sonnet-4",
        "google/gemini-2.5-pro", "x-ai/grok-4", "custom",
    ),
    "openai": ("gpt-5-mini", "gpt-5", "gpt-4o-mini", "custom"),
    "anthropic": ("claude-sonnet-4", "claude-opus-4", "custom"),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-pro", "custom"),
    "xai": ("grok-4", "grok-3", "custom"),
    "ollama": ("qwen3:8b", "qwen3:14b", "llama3.2", "custom"),
}
CHANNELS = ("voice", "telegram", "discord", "slack", "web", "imessage", "cli")
STRATEGIES = ("balanced", "quality", "fast", "cheap", "premium", "local_only")
STT_PROVIDERS = ("openai", "system", "none")
TTS_PROVIDERS = ("elevenlabs", "system", "none")
SECRET_ENV = {
    "openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY",
    "telegram": "TELEGRAM_BOT_TOKEN", "discord": "DISCORD_BOT_TOKEN",
    "slack": "SLACK_BOT_TOKEN",
}


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if CREDENTIALS_PATH.is_file():
        for line in CREDENTIALS_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _write_credentials(values: dict[str, str]) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={value}" for key, value in sorted(values.items()) if value]
    CREDENTIALS_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    CREDENTIALS_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    if secret and sys.stdin.isatty():
        value = getpass.getpass(f"{prompt}: ").strip()
    else:
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, StopIteration):
            return default
    return value or default


def _read_key() -> str:
    if not sys.stdin.isatty():
        return _ask("API key")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        data = ""
        while True:
            char = sys.stdin.read(1)
            if char in ("\r", "\n"):
                print()
                return data
            if char in ("\x7f", "\b"):
                if data:
                    data = data[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if char == "\x03":
                raise KeyboardInterrupt
            data += char
            sys.stdout.write("•")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _choose(title: str, options: tuple[str, ...], default: str) -> str:
    if not options:
        raise ValueError("no options")
    index = options.index(default) if default in options else 0
    print(f"\n◆ {title}")
    if not sys.stdin.isatty():
        return options[index]
    print("  ↑/↓ 이동   Enter 선택")
    while True:
        for i, option in enumerate(options):
            print(f"  {'❯' if i == index else ' '} {option}")
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            key = sys.stdin.read(1)
            if key == "\x1b":
                key += sys.stdin.read(2)
                if key == "\x1b[A": index = (index - 1) % len(options)
                elif key == "\x1b[B": index = (index + 1) % len(options)
            elif key in ("\r", "\n"):
                print()
                return options[index]
            elif key == "\x03":
                raise KeyboardInterrupt
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(f"\033[{len(options)}A")
        sys.stdout.flush()


def _multi(title: str, options: tuple[str, ...], defaults: list[str]) -> list[str]:
    selected = {item for item in defaults if item in options}
    if not selected and options:
        selected.add(options[0])
    index = 0
    print(f"\n◆ {title}")
    if not sys.stdin.isatty():
        return [option for option in options if option in selected]
    print("  ↑/↓ 이동   Space 선택   Enter 완료")
    while True:
        for i, option in enumerate(options):
            print(f"  {'❯' if i == index else ' '} {'●' if option in selected else '○'} {option}")
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            key = sys.stdin.read(1)
            if key == "\x1b":
                key += sys.stdin.read(2)
                if key == "\x1b[A": index = (index - 1) % len(options)
                elif key == "\x1b[B": index = (index + 1) % len(options)
            elif key == " ":
                if options[index] in selected:
                    selected.remove(options[index])
                else:
                    selected.add(options[index])
            elif key in ("\r", "\n"):
                print()
                return [option for option in options if option in selected]
            elif key == "\x03":
                raise KeyboardInterrupt
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(f"\033[{len(options)}A")
        sys.stdout.flush()


def _secret_for(provider: str, credentials: dict[str, str]) -> None:
    env_name = SECRET_ENV.get(provider)
    if not env_name or os.environ.get(env_name) or credentials.get(env_name):
        return
    print(f"\n{provider} API key가 필요합니다. 입력 내용은 화면에 표시되지 않습니다.")
    key = _read_key()
    if key:
        credentials[env_name] = key
        print("  저장했습니다.")
    else:
        print("  건너뜁니다.")


def _installed_names(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir() if path.is_dir())


def run() -> int:
    existing = _load(SETUP_PATH)
    current = _load(WORKSPACE_CONFIG)
    credentials = _env_file()

    old_provider = str(existing.get("default_provider") or current.get("providers", {}).get("default") or "openrouter")
    providers = _multi("사용할 Provider", PROVIDERS, list(existing.get("providers", [old_provider]))) or [old_provider]
    default_provider = _choose("기본 Provider", tuple(providers), old_provider if old_provider in providers else providers[0])
    _secret_for(default_provider, credentials)

    old_model = str(existing.get("model") or current.get("providers", {}).get("model") or "")
    model_options = MODELS[default_provider]
    model = _choose(f"{default_provider} 모델", model_options, old_model if old_model in model_options else model_options[0])
    if model == "custom":
        model = _ask("모델 ID")
    fallbacks = [item for item in providers if item != default_provider]
    for provider in fallbacks:
        _secret_for(provider, credentials)

    channels = _multi("연결할 Channel", CHANNELS, list(existing.get("channels", ["voice"]))) or ["voice"]
    for channel in channels:
        _secret_for(channel, credentials)

    voice_enabled = "voice" in channels
    stt = _choose("음성 입력(STT)", STT_PROVIDERS, str(existing.get("voice", {}).get("stt_provider", "openai"))) if voice_enabled else "none"
    tts = _choose("음성 출력(TTS)", TTS_PROVIDERS, str(existing.get("voice", {}).get("tts_provider", "elevenlabs"))) if voice_enabled else "none"
    if stt == "openai":
        _secret_for("openai", credentials)
    if tts == "elevenlabs":
        _secret_for("elevenlabs", credentials)
    voice_id = _ask("TTS Voice ID (선택)", str(existing.get("voice", {}).get("voice_id", ""))) if tts == "elevenlabs" else ""
    language = _ask("음성 언어", str(existing.get("voice", {}).get("language", "ko-KR")))

    strategy = _choose("오케스트레이션 전략", STRATEGIES, str(existing.get("orchestrator", {}).get("strategy", "balanced")))
    max_concurrency = int(_ask("동시 Agent 작업 수", str(existing.get("orchestrator", {}).get("max_concurrency", 4))))
    review = _ask("자동 Review 사용? (y/n)", "y").lower() in {"y", "yes"}
    auto_repair = _ask("자동 Repair 사용? (y/n)", "y").lower() in {"y", "yes"}

    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    setup_data = {
        "version": 5,
        "runtime": "native",
        "provider": default_provider,
        "providers": providers,
        "default_provider": default_provider,
        "model": model,
        "fallback_providers": fallbacks,
        "channels": channels,
        "voice": {"enabled": voice_enabled, "stt_provider": stt, "tts_provider": tts, "voice_id": voice_id, "language": language},
        "orchestrator": {"strategy": strategy, "backend": "native", "max_concurrency": max_concurrency, "review": review, "auto_repair": auto_repair},
        "plugins": _installed_names(PLUGIN_DIR),
        "skills": _installed_names(SKILL_DIR),
    }
    SETUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETUP_PATH.write_text(json.dumps(setup_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_credentials(credentials)

    current.setdefault("providers", {})["default"] = default_provider
    current["providers"]["model"] = model
    current["providers"]["fallbacks"] = fallbacks
    current.setdefault("routing", {})["strategy"] = strategy
    current.setdefault("agents", {})["default_backend"] = "native"
    current["agents"]["default_max_concurrency"] = max_concurrency
    current.setdefault("backends", {})["enabled"] = ["native"]
    current["voice"] = {"enabled": voice_enabled, "stt_provider": stt, "tts_provider": tts, "voice_id": voice_id, "language": language}
    current["channels"] = {"enabled": channels, "env": {channel: SECRET_ENV[channel] for channel in channels if channel in SECRET_ENV}}
    current.setdefault("workflow", {})["max_concurrency"] = max_concurrency
    current.setdefault("review", {})["enabled"] = review
    current.setdefault("repair", {})["max_retries"] = 3 if auto_repair else 0
    WORKSPACE_CONFIG.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n✓ AIRVIS setup complete")
    print(f"  Provider : {default_provider}")
    print(f"  Model    : {model}")
    print(f"  Channels : {', '.join(channels)}")
    print("  Secrets  : ~/.airvis/credentials.env (0600)")
    print("\nRun: airvis gateway")
    return 0


__all__ = ["run"]
