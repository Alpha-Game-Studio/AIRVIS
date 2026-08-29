"""AIRVIS voice channel: real microphone -> STT -> orchestrator -> TTS."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SETUP_PATH = Path.home() / ".airvis" / "setup.json"
CREDENTIALS_PATH = Path.home() / ".airvis" / "credentials.env"


def _credentials() -> dict[str, str]:
    values: dict[str, str] = {}
    if CREDENTIALS_PATH.is_file():
        for line in CREDENTIALS_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setup() -> dict[str, Any]:
    if not SETUP_PATH.is_file():
        return {}
    try:
        value = json.loads(SETUP_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _key(name: str) -> str:
    value = os.environ.get(name) or _credentials().get(name)
    if not value:
        raise RuntimeError(f"{name} is not configured; run `airvis setup` first")
    return value


def record(seconds: float = 6.0, sample_rate: int = 16000) -> Path:
    try:
        import sounddevice as sd
        import soundfile as sf
    except ImportError as exc:
        raise RuntimeError("voice dependencies missing; install `pip install -e '.[voice]'`") from exc
    print(f"🎙️  Listening ({seconds:g}s)…")
    audio = sd.rec(int(seconds * sample_rate), samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    path = Path(tempfile.gettempdir()) / f"airvis-{uuid.uuid4().hex}.wav"
    sf.write(path, audio, sample_rate, subtype="PCM_16")
    return path


def transcribe(path: Path, language: str = "ko") -> str:
    key = _key("OPENAI_API_KEY")
    boundary = "----AIRVIS" + uuid.uuid4().hex
    audio = path.read_bytes()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenAI STT failed ({exc.code}): {exc.read().decode(errors='replace')}") from exc
    return str(payload.get("text", "")).strip()


def speak(text: str, voice_id: str | None = None) -> Path:
    key = _key("ELEVENLABS_API_KEY")
    voice = voice_id or str(_setup().get("voice", {}).get("voice_id") or "21m00Tcm4TlvDq8ikWAM")
    model = str(_setup().get("voice", {}).get("tts_model") or "eleven_multilingual_v2")
    payload = json.dumps({
        "text": text,
        "model_id": model,
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
    }).encode()
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        data=payload,
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            audio = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ElevenLabs TTS failed ({exc.code}): {exc.read().decode(errors='replace')}") from exc
    path = Path(tempfile.gettempdir()) / f"airvis-tts-{uuid.uuid4().hex}.mp3"
    path.write_bytes(audio)
    play(path)
    return path


def play(path: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        command = ["afplay", str(path)]
    elif system == "Linux":
        command = ["mpv", "--no-video", "--really-quiet", str(path)]
    elif system == "Windows":
        command = ["powershell", "-NoProfile", "-Command", f"Start-Process -Wait '{path}'"]
    else:
        raise RuntimeError(f"unsupported audio platform: {system}")
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise RuntimeError(f"no audio player available for {system}") from exc


def run(engine: Any, seconds: float = 6.0, language: str = "ko", speak_answers: bool = True) -> int:
    setup = _setup().get("voice", {})
    if not setup.get("enabled", True):
        raise RuntimeError("voice channel is disabled; run `airvis setup voice`")
    print("AIRVIS Voice — native orchestration")
    print("Ctrl-C to stop.\n")
    while True:
        audio = record(seconds)
        try:
            text = transcribe(audio, language=language)
        finally:
            audio.unlink(missing_ok=True)
        if not text:
            print("… no speech detected")
            continue
        print(f"you › {text}")
        result = engine.run_sync(text)
        answer = str(getattr(result, "output", result)).strip()
        print(f"airvis › {answer}")
        if speak_answers and answer:
            speak(answer)
    return 0
