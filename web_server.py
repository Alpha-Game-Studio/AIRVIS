#!/usr/bin/env python3
"""Local control surface for AIRVIS.

The server intentionally uses only the Python standard library and binds to
localhost so desktop automation is never exposed to the network.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import config
import jarvis
from airvis.runtime import AgentRuntime


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
HOST = os.environ.get("AIRVIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("AIRVIS_PORT", "8765"))
API_TOKEN = os.environ.get("AIRVIS_API_TOKEN", "").strip()
COMMAND_LOCK = threading.Lock()
TTS_LOCK = threading.Lock()
TTS_ACTIVE = threading.Event()
NATIVE_RUNTIME = AgentRuntime()

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
    "JARVIS_STT_PROVIDER": "speech_recognition",
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
            output.append(f"{key}={values[key]}")
            handled.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in handled:
            output.append(f"{key}={value}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def update_settings(payload: dict[str, object]) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, (kind, _) in SETTING_DEFINITIONS.items():
        if name not in payload:
            continue
        value = payload[name]
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean")
        elif kind == "float":
            value = float(value)
            if value < 0:
                raise ValueError(f"{name} must be positive")
        else:
            value = str(value).strip()
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
            try:
                jarvis.speak_text(text)
            finally:
                TTS_ACTIVE.clear()

    threading.Thread(target=worker, daemon=True).start()


class AirvisHandler(BaseHTTPRequestHandler):
    server_version = "AIRVIS/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"[web] {format % args}")

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _authorized(self) -> bool:
        protected = bool(API_TOKEN) or HOST not in {"127.0.0.1", "localhost", "::1"}
        if not protected:
            return True
        header = self.headers.get("Authorization", "")
        return bool(API_TOKEN) and header == f"Bearer {API_TOKEN}"

    def do_GET(self) -> None:
        if not self._authorized():
            self._send_json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED)
            return
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json({
                "state": "busy" if jarvis._voice_session_active.is_set() else "standby",
                "tts": "speaking" if TTS_ACTIVE.is_set() else "idle",
                "engine": jarvis.get_current_engine(),
                "settings": settings_payload(),
                "time": time.time(),
                "native": NATIVE_RUNTIME.status(),
            })
            return
        if path == "/health":
            self._send_json({"ok": True})
            return
        if path == "/api/providers":
            self._send_json({"providers": NATIVE_RUNTIME.provider_manager.list()})
            return
        if path == "/api/models":
            self._send_json({"models": NATIVE_RUNTIME.catalog.list()})
            return
        if path == "/api/doctor":
            from airvis.doctor import run_checks
            self._send_json({"checks": run_checks()})
            return
        if path == "/api/costs":
            self._send_json({"total": NATIVE_RUNTIME.costs.total})
            return
        if path == "/api/tools":
            self._send_json({"tools": NATIVE_RUNTIME.tools.list()})
            return
        if path == "/api/memory":
            self._send_json({"memory": NATIVE_RUNTIME.memory.list()})
            return
        if path == "/api/sessions":
            self._send_json({"sessions": NATIVE_RUNTIME.sessions.list()})
            return
        if path == "/api/agents/status":
            self._send_json({"status": NATIVE_RUNTIME.status(), "agents": NATIVE_RUNTIME.agents.list()})
            return
        if path == "/api/tasks/cancel":
            NATIVE_RUNTIME.cancel()
            self._send_json({"cancelled": True, "status": NATIVE_RUNTIME.status()})
            return
        if path == "/api/tasks":
            self._send_json({"tasks": NATIVE_RUNTIME.task_list()})
            return
        if path == "/api/scheduler":
            self._send_json({"jobs": NATIVE_RUNTIME.scheduled_jobs()})
            return
        if path == "/api/agents":
            self._send_json({"agents": NATIVE_RUNTIME.agents.list()})
            return
        if path == "/api/plugins":
            self._send_json({"plugins": NATIVE_RUNTIME.plugins.list()})
            return
        if path == "/api/engine":
            self._send_json({"engine": jarvis.get_current_engine()})
            return
        if path == "/api/settings":
            self._send_json({"settings": settings_payload()})
            return
        file_path = WEB_DIR / ("index.html" if path == "/" else path.removeprefix("/"))
        if not file_path.is_file() or WEB_DIR not in file_path.parents:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8" if file_path.suffix == ".html" else "text/css; charset=utf-8" if file_path.suffix == ".css" else "application/javascript; charset=utf-8"
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            payload = self._read_json()
            if self.path == "/api/command":
                command = str(payload.get("command", "")).strip()
                if not command:
                    raise ValueError("command is required")
                with COMMAND_LOCK:
                    response = jarvis.handle_command(command)
                if payload.get("speak", False):
                    _speak_async(response)
                self._send_json({"response": response})
                return
            if self.path in {"/api/chat", "/api/agent/run"}:
                command = str(payload.get("message", payload.get("command", ""))).strip()
                if not command:
                    raise ValueError("message is required")
                self._send_json({"response": NATIVE_RUNTIME.run(command), "status": NATIVE_RUNTIME.status()})
                return
            if self.path == "/api/tasks":
                prompt = str(payload.get("prompt", "")).strip()
                if not prompt:
                    raise ValueError("prompt is required")
                task = NATIVE_RUNTIME.create_task(prompt)
                if payload.get("run", False):
                    response = NATIVE_RUNTIME.run_task(task.id)
                    self._send_json({"task": task.id, "response": response, "status": NATIVE_RUNTIME.status()})
                else:
                    self._send_json({"task": task.id, "tasks": NATIVE_RUNTIME.task_list()})
                return
            if self.path == "/api/scheduler":
                prompt = str(payload.get("prompt", "")).strip()
                delay = float(payload.get("delay_seconds", 0))
                if not prompt or delay < 0:
                    raise ValueError("prompt and a non-negative delay_seconds are required")
                self._send_json({"job": NATIVE_RUNTIME.schedule_once(prompt, delay)})
                return
            if self.path == "/api/scheduler/cancel":
                self._send_json({"cancelled": NATIVE_RUNTIME.cancel_job(str(payload.get("job", "")))})
                return
            if self.path == "/api/agents/delegate":
                role = str(payload.get("role", "")).strip()
                prompt = str(payload.get("prompt", "")).strip()
                if not role or not prompt:
                    raise ValueError("role and prompt are required")
                self._send_json({"response": NATIVE_RUNTIME.agents.delegate(role, prompt, NATIVE_RUNTIME)})
                return
            if self.path == "/api/tools/execute":
                result = NATIVE_RUNTIME.execute_tool(str(payload.get("name", "")), payload.get("arguments", {}), bool(payload.get("confirm", False)))
                self._send_json({"result": result, "status": NATIVE_RUNTIME.status()})
                return
            if self.path == "/api/memory":
                content = str(payload.get("content", "")).strip()
                if not content:
                    raise ValueError("content is required")
                self._send_json({"id": NATIVE_RUNTIME.memory.add(content)})
                return
            if self.path == "/api/speak":
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("text is required")
                _speak_async(text)
                self._send_json({"ok": True})
                return
            if self.path == "/api/engine":
                engine = str(payload.get("engine", "")).strip()
                if engine not in {"openclaw", "hermes", "grokbot"}:
                    raise ValueError("engine must be openclaw, hermes, or grokbot")
                response = jarvis.handle_engine_command(f"{engine} 엔진으로 전환")
                self._send_json({"engine": jarvis.get_current_engine(), "response": response})
                return
            if self.path == "/api/settings":
                self._send_json({"settings": update_settings(payload)}, HTTPStatus.OK)
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except PermissionError as exc:
            self._send_json({"error": str(exc), "confirmation_required": True}, HTTPStatus.CONFLICT)
        except Exception as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), AirvisHandler)
    print(f"AIRVIS control room: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAIRVIS server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()