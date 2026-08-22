from __future__ import annotations

import json
import logging
import shutil
import subprocess

from config import env_bool, env_int, env_str


log = logging.getLogger("openclaw_bridge")

OPENCLAW_UNAVAILABLE_MESSAGE = "OpenClaw에 연결할 수 없습니다."


def _extract_text_from_json(payload: object) -> str:
    """Recursively extract readable assistant text from OpenClaw JSON response."""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        # Specific OpenClaw JSON fields first
        for key in (
            "finalAssistantVisibleText",
            "finalAssistantRawText",
            "reply",
            "response",
            "message",
            "content",
            "text",
            "output",
        ):
            if key in payload and payload[key]:
                extracted = _extract_text_from_json(payload[key])
                if extracted:
                    return extracted
        for key in ("result", "data", "agent", "assistant", "payloads", "messages"):
            if key in payload and payload[key]:
                extracted = _extract_text_from_json(payload[key])
                if extracted:
                    return extracted
    if isinstance(payload, list):
        parts = [_extract_text_from_json(item) for item in payload]
        return "\n".join(part for part in parts if part).strip()
    return ""


def _openclaw_command(command: str) -> list[str]:
    cli = env_str("OPENCLAW_CLI", "openclaw")
    timeout = max(1, env_int("OPENCLAW_TIMEOUT", 120))
    args = [
        cli,
        "agent",
        "--message",
        command,
        "--json",
        "--timeout",
        str(timeout),
    ]
    # Default to 'main' agent unless overridden in env
    agent = env_str("OPENCLAW_AGENT", "main")
    session_key = env_str("OPENCLAW_SESSION_KEY", "jarvis")
    model = env_str("OPENCLAW_MODEL")

    if agent:
        args.extend(["--agent", agent])
    if session_key:
        args.extend(["--session-key", session_key])
    if model:
        args.extend(["--model", model])
    if env_bool("OPENCLAW_LOCAL", False):
        args.append("--local")
    return args


def ask_openclaw(command: str) -> str:
    """
    Send a text command to OpenClaw and return the agent response.

    This bridge uses the installed OpenClaw CLI because the local environment
    exposes `openclaw agent --agent main --message ... --json`. It safely
    handles CLI failures, timeouts, and JSON parsing errors without crashing.
    """
    command = command.strip()
    if not command:
        return "명령을 인식하지 못했습니다."

    cli = env_str("OPENCLAW_CLI", "openclaw")
    if shutil.which(cli) is None:
        log.warning("OpenClaw CLI not found: %s", cli)
        return OPENCLAW_UNAVAILABLE_MESSAGE

    timeout = env_int("OPENCLAW_TIMEOUT", 120)
    try:
        completed = subprocess.run(
            _openclaw_command(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        log.warning("OpenClaw request failed: %s", exc)
        return OPENCLAW_UNAVAILABLE_MESSAGE

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        log.warning("OpenClaw exited with code %s: %s", completed.returncode, stderr)
        return OPENCLAW_UNAVAILABLE_MESSAGE

    if not stdout:
        return "OpenClaw가 빈 응답을 반환했습니다."

    try:
        parsed = json.loads(stdout)
        text = _extract_text_from_json(parsed)
        return text or stdout
    except json.JSONDecodeError:
        return stdout
