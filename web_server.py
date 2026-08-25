#!/usr/bin/env python3
"""Local control surface and voice gateway for AIRVIS."""

from __future__ import annotations

import json
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent


def _load_airvis_credentials() -> None:
    """Load secrets created by ``airvis setup`` before voice/runtime imports."""
    path = Path.home() / ".airvis" / "credentials.env"
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and not os.environ.get(key):
                os.environ[key] = value
    except OSError:
        pass


_load_airvis_credentials()

import config
import jarvis
from airvis.core.asyncutil import run_blocking
from airvis.core.errors import ApprovalRequiredError, PermissionDeniedError
from airvis.runtime import AgentRuntime

WEB_DIR = BASE_DIR / "web"
HOST = os.environ.get("AIRVIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("AIRVIS_PORT", "8765"))
API_TOKEN = os.environ.get("AIRVIS_API_TOKEN", "").strip()
COMMAND_LOCK = threading.Lock()
TTS_LOCK = threading.Lock()
TTS_ACTIVE = threading.Event()
NATIVE_RUNTIME = AgentRuntime()
ENGINE = NATIVE_RUNTIME.engine

SETTING_DEFINITIONS = {
    "JARVIS_AGENT_ENABLED": ("bool", True),
    "JARVIS_CONVERSATION_MODE": ("bool", True),
    "JARVIS_WAKE_PROMPT": ("str", True),
    "JARVIS_STT_FAILURE_PROMPT": ("str", True),
    "JARVIS_STT_PROVIDER": ("str", True),
    "JARVIS_STT_LANGUAGE": ("str", True),
    "JARVIS_VAD_THRESHOLD": ("float", True),
    "JARVIS_VAD_SILENCE_SECONDS": ("float", True),
    "JARVIS_VAD_MAX_SECONDS": ("float", True),
    "JARVIS_VAD_TIMEOUT_SECONDS": ("float", True),
    "JARVIS_CHROME_URL": ("str", True),
    "JARVIS_YOUTUBE_URL": ("str", True),
    "SONG_URI": ("str", True),
    "OPENCLAW_AGENT": ("str", False),
    "OPENCLAW_TIMEOUT": ("float", False),
}
SETTING_DEFAULTS = {
    "JARVIS_STT_PROVIDER": "openai",
    "JARVIS_STT_LANGUAGE": "ko-KR",
    "OPENCLAW_AGENT": "main",
    "JARVIS_VAD_THRESHOLD": 0.025,
    "JARVIS_VAD_SILENCE_SECONDS": 0.6,
    "JARVIS_VAD_MAX_SECONDS": 12.0,
    "JARVIS_VAD_TIMEOUT_SECONDS": 8.0,
    "OPENCLAW_TIMEOUT": 120.0,
}


def _setting_value(name: str):
    kind, _ = SETTING_DEFINITIONS[name]
    if hasattr(jarvis, name):
        return getattr(jarvis, name)
    if kind == "bool":
        return config.env_bool(name, False)
    if kind == "float":
        return config.env_float(name, SETTING_DEFAULTS.get(name, 0.0))
    return config.env_str(name, SETTING_DEFAULTS.get(name, ""))


def settings_payload() -> dict[str, object]:
    return {name: _setting_value(name) for name in SETTING_DEFINITIONS}


def _write_env_values(values: dict[str, object]) -> None:
    env_path = BASE_DIR / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines()
    handled: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            output.append(f"{key}={values[key]}"); handled.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in handled: output.append(f"{key}={value}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def update_settings(payload: dict[str, object]) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, (kind, _) in SETTING_DEFINITIONS.items():
        if name not in payload: continue
        value = payload[name]
        if kind == "bool":
            if not isinstance(value, bool): raise ValueError(f"{name} must be boolean")
        elif kind == "float":
            value = float(value)
            if value < 0: raise ValueError(f"{name} must be positive")
        else: value = str(value).strip()
        values[name] = value
    _write_env_values(values)
    for name, value in values.items():
        setattr(jarvis, name, value)
        os.environ[name] = str(value).lower() if isinstance(value, bool) else str(value)
    return settings_payload()


def _speak_async(text: str) -> None:
    def worker() -> None:
        with TTS_LOCK:
            TTS_ACTIVE.set()
            try: jarvis.speak_text(text)
            finally: TTS_ACTIVE.clear()
    threading.Thread(target=worker, daemon=True).start()


class AirvisHandler(BaseHTTPRequestHandler):
    server_version = "AIRVIS/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"[web] {format % args}")

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(length) or b"{}")

    def _authorized(self) -> bool:
        protected = bool(API_TOKEN) or HOST not in {"127.0.0.1", "localhost", "::1"}
        if not protected: return True
        return self.headers.get("Authorization", "") == f"Bearer {API_TOKEN}"

    def do_GET(self) -> None:
        if not self._authorized(): self._send_json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED); return
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json({"state": "busy" if jarvis._voice_session_active.is_set() else "standby", "tts": "speaking" if TTS_ACTIVE.is_set() else "idle", "engine": jarvis.get_current_engine(), "settings": settings_payload(), "time": time.time(), "native": NATIVE_RUNTIME.status()}); return
        if path == "/health": self._send_json({"ok": True}); return
        if path == "/api/providers": self._send_json({"providers": NATIVE_RUNTIME.provider_manager.list()}); return
        if path == "/api/models": self._send_json({"models": NATIVE_RUNTIME.catalog.list()}); return
        if path == "/api/doctor":
            from airvis.doctor import run_checks, summarize
            self._send_json(summarize(run_checks(ENGINE))); return
        if path == "/api/backends": self._send_json({"backends": ENGINE.backends.list()}); return
        if path == "/api/health": self._send_json(run_blocking(ENGINE.health_check())); return
        if path == "/api/workflows": self._send_json({"workflows": ENGINE.store.list_workflows()}); return
        if path == "/api/events": self._send_json({"events": ENGINE.event_bus.history(limit=200)}); return
        if path == "/api/config": self._send_json({"config": ENGINE.config.to_dict()}); return
        if path == "/api/costs": self._send_json({"total": NATIVE_RUNTIME.costs.total}); return
        if path == "/api/tools": self._send_json({"tools": NATIVE_RUNTIME.tools.list()}); return
        if path == "/api/memory": self._send_json({"memory": NATIVE_RUNTIME.memory.list()}); return
        if path == "/api/sessions": self._send_json({"sessions": NATIVE_RUNTIME.sessions.list()}); return
        if path == "/api/agents/status": self._send_json({"status": NATIVE_RUNTIME.status(), "agents": NATIVE_RUNTIME.agents.list()}); return
        if path == "/":
            index = WEB_DIR / "index.html"
            if index.is_file():
                body = index.read_bytes(); self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._authorized(): self._send_json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED); return
        path = urlparse(self.path).path
        try: payload = self._read_json()
        except (ValueError, json.JSONDecodeError): self._send_json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST); return
        if path == "/api/chat":
            message = str(payload.get("message", "")).strip()
            if not message: self._send_json({"error": "message is required"}, HTTPStatus.BAD_REQUEST); return
            try:
                with COMMAND_LOCK: result = NATIVE_RUNTIME.run(message)
                if result: _speak_async(result)
                self._send_json({"ok": True, "text": result})
            except (ApprovalRequiredError, PermissionDeniedError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.FORBIDDEN)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if path == "/api/settings":
            try: self._send_json({"ok": True, "settings": update_settings(payload)})
            except ValueError as exc: self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> None:
    ThreadingHTTPServer((HOST, PORT), AirvisHandler).serve_forever()


if __name__ == "__main__": main()
