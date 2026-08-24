from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from config import env_bool, env_int, env_str
from openclaw_bridge import ask_openclaw


log = logging.getLogger("engine_bridge")

# AIRVIS is a self-contained agent engine. External engines remain optional
# compatibility backends, but they are never the default execution path.
_current_engine = env_str("AI_ENGINE", "native").lower()
_native_runtime = None


def get_current_engine() -> str:
    global _current_engine
    return _current_engine or "native"


def set_current_engine(engine_name: str) -> str:
    global _current_engine
    e = engine_name.lower().strip()
    if e in {"native", "airvis", "native-agent", "airvis-agent"}:
        _current_engine = "native"
        return "AIRVIS Native Agent"
    if e in {"hermes", "에르메스", "nous", "nous-hermes", "hermes-agent"}:
        _current_engine = "hermes"
        return "에르메스(Hermes Agent)"
    if e in {"grok", "grokbot", "그록", "그록봇", "xai", "grok-bot"}:
        _current_engine = "grokbot"
        return "그록봇(Grok Bot)"
    if e in {"openclaw", "오픈클로", "claw", "clawbot"}:
        _current_engine = "openclaw"
        return "오픈클로(OpenClaw)"
    _current_engine = e
    return engine_name


def _find_local_binary(names: list[str]) -> str | None:
    """Find a local executable across PATH and common user installation paths."""
    home = Path.home()
    custom_paths = [home / ".hermes" / "bin", home / ".local" / "bin", home / "bin",
                    Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        for directory in custom_paths:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _http_chat_completion(base_url: str, api_key: str, model: str,
                          system_prompt: str, user_prompt: str, timeout: int = 30) -> str | None:
    try:
        import requests
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if "openrouter" in base_url.lower():
            headers["HTTP-Referer"] = "https://github.com/Alpha-Game-Studio/AIRVIS"
            headers["X-Title"] = "AIRVIS AI Assistant"
        response = requests.post(url, headers=headers, json={
            "model": model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_prompt}],
            "temperature": 0.7,
        }, timeout=timeout)
        if response.status_code == 200:
            choices = response.json().get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if content:
                    return content.strip()
        else:
            log.warning("HTTP chat completion error (%s): %s", response.status_code, response.text[:200])
    except Exception as exc:
        log.warning("HTTP chat completion failed: %s", exc)
    return None


# Hermes and Grok remain optional compatibility engines. They are intentionally
# kept here so switching engines does not contaminate the AIRVIS native kernel.
def ask_hermes(command: str) -> str:
    command = command.strip()
    if not command:
        return "명령을 인식하지 못했습니다."
    timeout = env_int("HERMES_TIMEOUT", 60)
    hermes_bin = _find_local_binary(["hermes", "hermes-agent"])
    if hermes_bin:
        for args in ([hermes_bin, "chat", "--message", command],
                     [hermes_bin, "--message", command], [hermes_bin, "run", command],
                     [hermes_bin, command]):
            try:
                completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
                if completed.returncode == 0 and completed.stdout.strip():
                    return completed.stdout.strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("Hermes CLI error: %s", exc)
    api_key = env_str("HERMES_API_KEY") or env_str("OPENROUTER_API_KEY") or env_str("NOUS_API_KEY")
    base_url = env_str("HERMES_BASE_URL")
    model = env_str("HERMES_MODEL", "nousresearch/hermes-3-llama-3.1-405b")
    system_prompt = env_str("HERMES_SYSTEM_PROMPT", "You are Hermes Agent. Reply in natural Korean.")
    if api_key:
        base_url = base_url or "https://openrouter.ai/api/v1"
        result = _http_chat_completion(base_url, api_key, model, system_prompt, command, timeout)
        if result:
            return result
    return "에르메스 에이전트에 연결할 수 없습니다."


def ask_grokbot(command: str) -> str:
    command = command.strip()
    if not command:
        return "명령을 인식하지 못했습니다."
    timeout = env_int("GROK_TIMEOUT", 60)
    api_key = env_str("XAI_API_KEY") or env_str("GROK_API_KEY") or env_str("OPENROUTER_API_KEY")
    base_url = env_str("GROK_BASE_URL")
    model = env_str("GROK_MODEL", "grok-2-latest")
    if api_key:
        if not base_url:
            base_url = "https://api.x.ai/v1" if api_key.startswith("xai-") else "https://openrouter.ai/api/v1"
            if "openrouter" in base_url and model == "grok-2-latest":
                model = "x-ai/grok-2"
        result = _http_chat_completion(base_url, api_key,
                                       model, "You are Grok Bot. Reply in natural Korean.", command, timeout)
        if result:
            return result
    return "그록봇에 연결할 수 없습니다."


def ask_ai_engine(command: str, engine: str | None = None) -> str:
    """Dispatch to the selected engine; AIRVIS Native is the default and primary path."""
    active = (engine or get_current_engine()).lower().strip()
    if active in {"native", "airvis", "native-agent", "airvis-agent"}:
        global _native_runtime
        if _native_runtime is None:
            from airvis.runtime import AgentRuntime
            _native_runtime = AgentRuntime()
        return _native_runtime.run(command)
    if active in {"hermes", "에르메스", "hermes-agent"}:
        return ask_hermes(command)
    if active in {"grok", "grokbot", "그록", "그록봇", "grok-bot"}:
        return ask_grokbot(command)
    if active in {"openclaw", "오픈클로", "claw", "clawbot"}:
        return ask_openclaw(command)
    log.warning("Unknown engine '%s'; falling back to AIRVIS Native Agent", active)
    return ask_ai_engine(command, "native")
