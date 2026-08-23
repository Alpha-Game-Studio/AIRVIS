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

# Current active engine (native | openclaw | hermes | grokbot)
_current_engine = env_str("AI_ENGINE", "openclaw").lower()

# Lazily created AIRVIS native runtime, shared across calls.
_native_runtime = None


def get_current_engine() -> str:
    global _current_engine
    return _current_engine or "openclaw"


def set_current_engine(engine_name: str) -> str:
    global _current_engine
    e = engine_name.lower().strip()
    if e in {"native", "airvis", "native-agent", "airvis-agent"}:
        _current_engine = "native"
        return "AIRVIS Native Agent"
    elif e in {"hermes", "에르메스", "nous", "nous-hermes", "hermes-agent"}:
        _current_engine = "hermes"
        return "에르메스(Hermes Agent)"
    elif e in {"grok", "grokbot", "그록", "그록봇", "xai", "grok-bot"}:
        _current_engine = "grokbot"
        return "그록봇(Grok Bot)"
    elif e in {"openclaw", "오픈클로", "claw", "clawbot"}:
        _current_engine = "openclaw"
        return "오픈클로(OpenClaw)"
    else:
        _current_engine = e
        return engine_name


def _find_local_binary(names: list[str]) -> str | None:
    """Find a local executable across system PATH and standard user installation directories."""
    home = Path.home()
    custom_paths = [
        home / ".hermes" / "bin",
        home / ".local" / "bin",
        home / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    for name in names:
        # Check standard PATH
        found = shutil.which(name)
        if found:
            return found
        # Check standard local directories
        for p in custom_paths:
            candidate = p / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _http_chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 30,
) -> str | None:
    """Generic OpenAI-compatible chat completion helper using requests."""
    try:
        import requests

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # Special headers for OpenRouter
        if "openrouter" in base_url.lower():
            headers["HTTP-Referer"] = "https://github.com/cuufi2fh-png/AIRVIS"
            headers["X-Title"] = "AIRVIS AI Assistant"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
        }

        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code == 200:
            data = response.json()
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if content:
                    return content.strip()
        else:
            log.warning(
                "HTTP chat completion error (%s): %s",
                response.status_code,
                response.text[:200],
            )
    except Exception as exc:
        log.warning("HTTP chat completion failed: %s", exc)
    return None


# --- Hermes Agent (Nous Research: https://hermes-agent.nousresearch.com/docs) ---

def ask_hermes(command: str) -> str:
    """
    Query Hermes Agent (Nous Research).
    Priority:
      1. Local Hermes CLI (`hermes`, `hermes-agent`) if installed on host
      2. Direct Nous Portal / OpenRouter / Ollama / Local API
      3. OpenClaw with Hermes model routing (if OpenClaw is running)
      4. Safe fallback message without crashing
    """
    command = command.strip()
    if not command:
        return "명령을 인식하지 못했습니다."

    timeout = env_int("HERMES_TIMEOUT", 60)

    # 1. Check local Hermes CLI
    hermes_bin = _find_local_binary(["hermes", "hermes-agent"])
    if hermes_bin:
        # Try standard Hermes CLI invocation modes
        for args in (
            [hermes_bin, "chat", "--message", command],
            [hermes_bin, "--message", command],
            [hermes_bin, "run", command],
            [hermes_bin, command],
        ):
            try:
                completed = subprocess.run(
                    args,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if completed.returncode == 0 and completed.stdout.strip():
                    return completed.stdout.strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("Hermes CLI error: %s", exc)

    # 2. Check Direct API / OpenRouter / Nous Portal / Local Endpoint
    api_key = env_str("HERMES_API_KEY") or env_str("OPENROUTER_API_KEY") or env_str("NOUS_API_KEY")
    base_url = env_str("HERMES_BASE_URL")
    model = env_str("HERMES_MODEL", "nousresearch/hermes-3-llama-3.1-405b")
    system_prompt = env_str(
        "HERMES_SYSTEM_PROMPT",
        "You are Hermes Agent by Nous Research. You are autonomous, highly intelligent, and concise. Reply in natural Korean.",
    )

    if api_key:
        if not base_url:
            base_url = "https://openrouter.ai/api/v1"
        res = _http_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_prompt=command,
            timeout=timeout,
        )
        if res:
            return res

    if base_url:
        res = _http_chat_completion(
            base_url=base_url,
            api_key="none",
            model=model,
            system_prompt=system_prompt,
            user_prompt=command,
            timeout=timeout,
        )
        if res:
            return res

    # 3. Route through local OpenClaw gateway if available
    openclaw_bin = _find_local_binary(["openclaw"])
    if openclaw_bin:
        try:
            completed = subprocess.run(
                [
                    openclaw_bin,
                    "agent",
                    "--agent",
                    env_str("HERMES_OPENCLAW_AGENT", "main"),
                    "--model",
                    model,
                    "--message",
                    command,
                    "--json",
                    "--timeout",
                    str(timeout),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                from openclaw_bridge import _extract_text_from_json

                try:
                    data = json.loads(completed.stdout)
                    text = _extract_text_from_json(data)
                    if text:
                        return text
                except Exception:
                    return completed.stdout.strip()
        except Exception as exc:
            log.warning("OpenClaw Hermes routing error: %s", exc)

    # 4. If all fail, return clean notification
    return "에르메스 에이전트에 연결할 수 없습니다. hermes CLI 설치 또는 HERMES_API_KEY 설정을 확인해주세요."


# --- Grok Bot (xAI: https://docs.x.ai/grok-bot/overview) ---------------------

def ask_grokbot(command: str) -> str:
    """
    Query Grok Bot (xAI).
    Priority:
      1. Local Grok Bot CLI (`grokbot`, `grok`) if installed on host
      2. Direct xAI API (`https://api.x.ai/v1`, models `grok-4.20-multi-agent`, `grok-2-latest`) / OpenRouter
      3. OpenClaw with Grok model routing (if OpenClaw is running)
      4. Safe fallback message without crashing
    """
    command = command.strip()
    if not command:
        return "명령을 인식하지 못했습니다."

    timeout = env_int("GROK_TIMEOUT", 60)

    # 1. Check local Grok Bot CLI
    grok_bin = _find_local_binary(["grokbot", "grok"])
    if grok_bin:
        for args in (
            [grok_bin, "--message", command],
            [grok_bin, "chat", "--message", command],
            [grok_bin, command],
        ):
            try:
                completed = subprocess.run(
                    args,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if completed.returncode == 0 and completed.stdout.strip():
                    return completed.stdout.strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("Grok Bot CLI error: %s", exc)

    # 2. Direct xAI API / OpenRouter
    api_key = env_str("XAI_API_KEY") or env_str("GROK_API_KEY") or env_str("OPENROUTER_API_KEY")
    base_url = env_str("GROK_BASE_URL")
    model = env_str("GROK_MODEL", "grok-2-latest")
    system_prompt = env_str(
        "GROK_SYSTEM_PROMPT",
        "You are Grok Bot by xAI. You are an autonomous, intelligent, witty, and concise AI teammate. Reply in natural Korean.",
    )

    if api_key:
        if not base_url:
            if api_key.startswith("xai-"):
                base_url = "https://api.x.ai/v1"
            else:
                base_url = "https://openrouter.ai/api/v1"
                if model == "grok-2-latest":
                    model = "x-ai/grok-2"

        res = _http_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_prompt=command,
            timeout=timeout,
        )
        if res:
            return res

    # 3. Route through local OpenClaw gateway if available
    openclaw_bin = _find_local_binary(["openclaw"])
    if openclaw_bin:
        try:
            completed = subprocess.run(
                [
                    openclaw_bin,
                    "agent",
                    "--agent",
                    env_str("GROK_OPENCLAW_AGENT", "main"),
                    "--model",
                    env_str("GROK_OPENCLAW_MODEL", "x-ai/grok-2"),
                    "--message",
                    command,
                    "--json",
                    "--timeout",
                    str(timeout),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                from openclaw_bridge import _extract_text_from_json

                try:
                    data = json.loads(completed.stdout)
                    text = _extract_text_from_json(data)
                    if text:
                        return text
                except Exception:
                    return completed.stdout.strip()
        except Exception as exc:
            log.warning("OpenClaw Grok routing error: %s", exc)

    # 4. If all fail, return clean notification
    return "그록봇에 연결할 수 없습니다. xAI API 키 또는 grokbot 설정을 확인해주세요."


# --- Unified AI Engine Dispatcher -------------------------------------------

def ask_ai_engine(command: str, engine: str | None = None) -> str:
    """
    Route command to the active AI Engine (OpenClaw, Hermes, or Grokbot).
    """
    active = (engine or get_current_engine()).lower().strip()

    if active in {"native", "airvis", "native-agent"}:
        global _native_runtime
        if _native_runtime is None:
            from airvis.runtime import AgentRuntime

            _native_runtime = AgentRuntime()
        log.info("Querying AIRVIS native orchestration engine: %s", command)
        return _native_runtime.run(command)

    if active in {"hermes", "에르메스", "hermes-agent"}:
        log.info("Querying Hermes Agent: %s", command)
        return ask_hermes(command)
    if active in {"grok", "grokbot", "그록", "그록봇", "grok-bot"}:
        log.info("Querying Grok Bot: %s", command)
        return ask_grokbot(command)
    log.info("Querying OpenClaw Engine: %s", command)
    return ask_openclaw(command)
