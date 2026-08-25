"""Build providers from configuration and the environment."""

from __future__ import annotations

import os
from typing import Any

from ..core.config import AirvisConfig, ProvidersConfig
from ..core.errors import ConfigError
from .base import Provider
from .http import AnthropicProvider, GeminiProvider, OpenAICompatibleProvider
from .mock import MockProvider
from .registry import ProviderRegistry

OPENAI_COMPATIBLE: dict[str, dict[str, Any]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "model_env": "OPENAI_MODEL", "model": "gpt-4o-mini", "quality": 0.85, "cost_in": 0.15, "cost_out": 0.6},
    "xai": {"base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY", "model_env": "GROK_MODEL", "model": "grok-2-latest", "quality": 0.8, "cost_in": 2.0, "cost_out": 10.0},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "model_env": "OPENROUTER_MODEL", "model": "openai/gpt-5-mini", "quality": 0.78, "cost_in": 0.9, "cost_out": 0.9},
    "ollama": {"base_url": "", "key_env": "", "model_env": "OLLAMA_MODEL", "model": "llama3.2", "local": True, "quality": 0.45, "cost_in": 0.0, "cost_out": 0.0},
    "custom": {"base_url": "", "key_env": "AIRVIS_API_KEY", "model_env": "AIRVIS_MODEL", "model": "gpt-4o-mini", "quality": 0.6, "cost_in": 0.0, "cost_out": 0.0},
}

LOCAL_ALIASES = {"local", "ollama"}


def build_provider(provider_id: str, environ: dict[str, str] | None = None, *, timeout: float = 60.0, model: str = "") -> Provider:
    env = dict(os.environ if environ is None else environ)
    token = provider_id.strip().lower()
    if token in {"", "mock", "native", "airvis"}:
        return MockProvider()
    if token in LOCAL_ALIASES:
        host = env.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        spec = OPENAI_COMPATIBLE["ollama"]
        selected_model = model or env.get(spec["model_env"], spec["model"])
        return OpenAICompatibleProvider("ollama", f"{host}/v1", api_key="ollama", model=selected_model, timeout=timeout, local=True, quality=spec["quality"])
    if token == "anthropic":
        selected_model = model or env.get("ANTHROPIC_MODEL", "")
        return AnthropicProvider(api_key=env.get("ANTHROPIC_API_KEY", ""), model=selected_model, timeout=timeout)
    if token in {"gemini", "google"}:
        selected_model = model or env.get("GEMINI_MODEL", "")
        return GeminiProvider(api_key=env.get("GOOGLE_API_KEY", "") or env.get("GEMINI_API_KEY", ""), model=selected_model, timeout=timeout)
    compatible = OPENAI_COMPATIBLE.get(token)
    if compatible is None:
        raise ConfigError(f"unknown provider id: {provider_id}", known=sorted({*OPENAI_COMPATIBLE, "anthropic", "gemini", "mock"}))
    spec = compatible
    base_url = spec["base_url"] or env.get("AIRVIS_CUSTOM_BASE_URL", "http://127.0.0.1:8000/v1")
    headers = {"HTTP-Referer": "https://github.com/Alpha-Game-Studio/AIRVIS", "X-Title": "AIRVIS"} if token == "openrouter" else {}
    selected_model = model or env.get(spec["model_env"], spec["model"])
    return OpenAICompatibleProvider(token, base_url, api_key=env.get(spec["key_env"], "") if spec["key_env"] else "", model=selected_model, timeout=timeout, local=bool(spec.get("local")), quality=spec["quality"], cost_per_million_input=spec["cost_in"], cost_per_million_output=spec["cost_out"], extra_headers=headers)


def discover_provider_ids(environ: dict[str, str] | None = None) -> list[str]:
    env = dict(os.environ if environ is None else environ)
    found: list[str] = []
    if env.get("OPENAI_API_KEY"): found.append("openai")
    if env.get("ANTHROPIC_API_KEY"): found.append("anthropic")
    if env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY"): found.append("gemini")
    if env.get("XAI_API_KEY"): found.append("xai")
    if env.get("OPENROUTER_API_KEY"): found.append("openrouter")
    if env.get("OLLAMA_HOST"): found.append("ollama")
    return found


def build_provider_registry(config: AirvisConfig | ProvidersConfig | None = None, *, environ: dict[str, str] | None = None, health: Any = None, event_bus: Any = None) -> ProviderRegistry:
    providers_config = config.providers if isinstance(config, AirvisConfig) else (config or ProvidersConfig())
    env = dict(os.environ if environ is None else environ)
    registry = ProviderRegistry(health=health, event_bus=event_bus)

    explicit = bool(providers_config.default.strip())
    wanted: list[str] = []
    if explicit:
        wanted.append(providers_config.default.strip().lower())
    wanted.extend(item.strip().lower() for item in providers_config.fallbacks if item.strip())
    wanted.extend(discover_provider_ids(env))

    seen: set[str] = set()
    for provider_id in wanted:
        canonical = "ollama" if provider_id in LOCAL_ALIASES else provider_id
        if canonical in seen:
            continue
        seen.add(canonical)
        try:
            selected_model = providers_config.model if canonical == providers_config.default.strip().lower() else ""
            registry.register(build_provider(provider_id, env, timeout=providers_config.request_timeout, model=selected_model))
        except ConfigError:
            continue

    if not explicit and len(registry) == 0:
        registry.register(MockProvider())

    ordered_fallbacks = [item for item in (providers_config.fallbacks or []) if registry.has(item)]
    if not explicit and "mock" in registry and "mock" not in ordered_fallbacks:
        ordered_fallbacks.append("mock")
    registry.fallbacks = ordered_fallbacks
    return registry


def provider_from_environment(environ: dict[str, str] | None = None) -> Provider:
    env = dict(os.environ if environ is None else environ)
    selected = env.get("AIRVIS_PROVIDER", "").strip()
    model = env.get("AIRVIS_MODEL", "").strip()
    if not selected:
        discovered = discover_provider_ids(env)
        selected = discovered[0] if discovered else "mock"
    try:
        return build_provider(selected, env, model=model)
    except ConfigError:
        return MockProvider()


__all__ = ["build_provider", "build_provider_registry", "discover_provider_ids", "provider_from_environment"]
