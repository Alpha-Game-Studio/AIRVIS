from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any

from config import env_bool, env_int, env_str
from openclaw_bridge import ask_openclaw


log = logging.getLogger("engine_bridge")

# Current active engine (openclaw | hermes | grokbot)
_current_engine = env_str("AI_ENGINE", "openclaw").lower()


def get_current_engine() -> str:
    global _current_engine
    return _current_engine or "openclaw"


def set_current_engine(engine_name: str) -> str:
    global _current_engine
    e = engine_name.lower().strip()
    if e in {"hermes", "에르메스", "nous", "nous-hermes"}:
        _current_engine = "hermes"
        return "에르메스(Hermes)"
    elif e in {"grok", "grokbot", "그록", "그록봇", "xai"}:
        _current_engine = "grokbot"
        return "그록봇(Grokbot)"
    elif e in {"openclaw", "오픈클로", "claw", "clawbot"}:
        _current_engine = "openclaw"
        return "오픈클로(OpenClaw)"
    else:
        _current_engine = e
        return engine_name


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
        # Special header for OpenRouter
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
            log.warning("HTTP chat completion error (%s): %s", response.status_code, response.text[:200])
    except Exception as exc:
        log.warning("HTTP chat completion failed: %s", exc)
    return None


# --- Hermes Engine ----------------------------------------------------------

def ask_hermes(command: str) -> str:
    """
    Query Hermes (Nous Research) AI Engine.
    Priority:
      1. Direct API / OpenRouter / Ollama / Local API
      2. OpenClaw with Hermes model / agent
      3. Hermes CLI if available
      4. Fallback to OpenClaw default
    """
    command = command.strip()
    if not command:
        return "명령을 인식하지 못했습니다."

    system_prompt = env_str(
        "HERMES_SYSTEM_PROMPT",
        "You are Hermes, a helpful, intelligent, and concise AI assistant. Reply in natural Korean.",
    )
    api_key = env_str("HERMES_API_KEY") or env_str("OPENROUTER_API_KEY")
    base_url = env_str("HERMES_BASE_URL")
    model = env_str("HERMES_MODEL", "nousresearch/hermes-3-llama-3.1-405b")

    # 1. Direct API if configured
    if api_key:
        if not base_url:
            base_url = "https://openrouter.ai/api/v1"
        res = _http_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_prompt=command,
        )
        if res:
            return res

    # 2. Local Ollama / vLLM if base_url is specified without key
    if base_url:
        res = _http_chat_completion(
            base_url=base_url,
            api_key="none",
            model=model,
            system_prompt=system_prompt,
            user_prompt=command,
        )
        if res:
            return res

    # 3. Hermes CLI
    cli = env_str("HERMES_CLI", "hermes")
    if shutil.which(cli) is not None:
        try:
            completed = subprocess.run(
                [cli, "chat", "--message", command],
                check=False,
                capture_output=True,
                text=True,
                timeout=env_int("HERMES_TIMEOUT", 60),
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return completed.stdout.strip()
        except Exception as exc:
            log.warning("Hermes CLI failed: %s", exc)

    # 4. OpenClaw with Hermes Model routing
    cli_openclaw = env_str("OPENCLAW_CLI", "openclaw")
    if shutil.which(cli_openclaw) is not None:
        try:
            completed = subprocess.run(
                [
                    cli_openclaw,
                    "agent",
                    "--agent",
                    env_str("HERMES_OPENCLAW_AGENT", "main"),
                    "--model",
                    model,
                    "--message",
                    command,
                    "--json",
                    "--timeout",
                    str(env_int("HERMES_TIMEOUT", 120)),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=env_int("HERMES_TIMEOUT", 120) + 5,
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
            log.warning("OpenClaw Hermes routing failed: %s", exc)

    # 5. Default OpenClaw fallback
    return ask_openclaw(command)


# --- Grokbot (xAI Grok) Engine ----------------------------------------------

def ask_grokbot(command: str) -> str:
    """
    Query Grokbot (xAI Grok) AI Engine.
    Priority:
      1. xAI API / OpenRouter
      2. Grokbot CLI if available
      3. OpenClaw with Grok model routing
      4. Fallback to OpenClaw default
    """
    command = command.strip()
    if not command:
        return "명령을 인식하지 못했습니다."

    system_prompt = env_str(
        "GROK_SYSTEM_PROMPT",
        "You are Grok, an AI created by xAI. You are witty, knowledgeable, and helpful. Reply in natural Korean.",
    )
    api_key = env_str("XAI_API_KEY") or env_str("GROK_API_KEY") or env_str("OPENROUTER_API_KEY")
    base_url = env_str("GROK_BASE_URL")
    model = env_str("GROK_MODEL", "grok-2-latest")

    # 1. Direct xAI API / OpenRouter
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
        )
        if res:
            return res

    # 2. Grokbot CLI if installed
    for cli_name in ("grokbot", "grok"):
        if shutil.which(cli_name) is not None:
            try:
                completed = subprocess.run(
                    [cli_name, "--message", command],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=env_int("GROK_TIMEOUT", 60),
                )
                if completed.returncode == 0 and completed.stdout.strip():
                    return completed.stdout.strip()
            except Exception as exc:
                log.warning("Grokbot CLI failed: %s", exc)

    # 3. OpenClaw with Grok model routing
    cli_openclaw = env_str("OPENCLAW_CLI", "openclaw")
    if shutil.which(cli_openclaw) is not None:
        try:
            completed = subprocess.run(
                [
                    cli_openclaw,
                    "agent",
                    "--agent",
                    env_str("GROK_OPENCLAW_AGENT", "main"),
                    "--model",
                    env_str("GROK_OPENCLAW_MODEL", "x-ai/grok-2"),
                    "--message",
                    command,
                    "--json",
                    "--timeout",
                    str(env_int("GROK_TIMEOUT", 120)),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=env_int("GROK_TIMEOUT", 120) + 5,
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
            log.warning("OpenClaw Grok routing failed: %s", exc)

    # 4. Default OpenClaw fallback
    return ask_openclaw(command)


# --- Unified AI Engine Dispatcher -------------------------------------------

def ask_ai_engine(command: str, engine: str | None = None) -> str:
    """
    Route command to the active AI Engine (OpenClaw, Hermes, or Grokbot).
    """
    active = (engine or get_current_engine()).lower().strip()

    if active in {"hermes", "에르메스"}:
        log.info("Querying Hermes Engine: %s", command)
        return ask_hermes(command)
    elif active in {"grok", "grokbot", "그록", "그록봇"}:
        log.info("Querying Grokbot Engine: %s", command)
        return ask_grokbot(command)
    else:
        log.info("Querying OpenClaw Engine: %s", command)
        return ask_openclaw(command)
