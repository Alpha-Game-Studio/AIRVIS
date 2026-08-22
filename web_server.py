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


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
HOST = os.environ.get("AIRVIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("AIRVIS_PORT", "8765"))
COMMAND_LOCK = threading.Lock()

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

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json({
                "state": "busy" if jarvis._voice_session_active.is_set() else "standby",
                "settings": settings_payload(),
                "time": time.time(),
            })
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
        try:
            payload = self._read_json()
            if self.path == "/api/command":
                command = str(payload.get("command", "")).strip()
                if not command:
                    raise ValueError("command is required")
                with COMMAND_LOCK:
                    response = jarvis.handle_command(command)
                self._send_json({"response": response})
                return
            if self.path == "/api/speak":
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("text is required")
                threading.Thread(target=jarvis.speak_text, args=(text,), daemon=True).start()
                self._send_json({"ok": True})
                return
            if self.path == "/api/settings":
                self._send_json({"settings": update_settings(payload)}, HTTPStatus.OK)
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
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