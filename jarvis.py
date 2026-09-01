#!/usr/bin/env python3
"""
Desktop Double-Clap Listener × Jarvis AI Assistant (OpenClaw + ElevenLabs + STT)

Workflow:
  Double Clap (👏 👏)
    ↓
  Jarvis Wake-up ("Yes, sir. 무엇을 도와드릴까요?")
    ↓
  Voice Input & STT
    ↓
  Command Routing (Local Automation vs. OpenClaw Agent)
    ↓
  Execution & Response Generation
    ↓
  ElevenLabs TTS Playback
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
import webbrowser
from pathlib import Path

import numpy as np
import sounddevice as sd

from config import env_bool, env_float, env_int, env_str
from openclaw_bridge import ask_openclaw
from speech import listen_and_transcribe, speak_text


# --- Double-Clap Tuning Knobs -----------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40
CHANNELS = 1

SPIKE_RATIO = 7.0
COOLDOWN_S = 0.45
MIN_DOUBLE_GAP_S = 0.05
MAX_DOUBLE_GAP_S = 0.35
RETRIGGER_RATIO = 0.55
NOISE_FLOOR_ALPHA = 0.992
MIN_RMS = 0.012
QUIET_GATE_MULT = 2.2
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001

# --- Legacy Actions & Integrations ------------------------------------------
SONG_URI = env_str(
    "SONG_URI",
    "https://open.spotify.com/track/39shmbIHICJ2Wxnk1fPSdz?si=2900c75c2e2d4b82",
)
FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP = True
OPEN_NEW_CURSOR_ON_DOUBLE_CLAP = False
CURSOR_OPEN_FULLSCREEN = True

OPEN_CLAUDE_CODE_IN_CHROME = True
OPEN_BINANCE_BTC_IN_CHROME = True
OPEN_CHROME_FULLSCREEN = True
CHROME_SEPARATE_SITE_PROFILES = False
CLAUDE_CHROME_MONITOR = 1
BINANCE_CHROME_MONITOR = 3

JARVIS_WELCOME_ENABLED = env_bool("JARVIS_WELCOME_ENABLED", False)
JARVIS_WELCOME_PHRASE = env_str(
    "JARVIS_WELCOME_PHRASE",
    "Welcome home sir. Jarvis system is fully operational.",
)
JARVIS_AFTER_SONG_DELAY_S = env_float("JARVIS_AFTER_SONG_DELAY_S", 1.0)
JARVIS_WELCOME_CACHE_ENABLED = env_bool("JARVIS_WELCOME_CACHE_ENABLED", True)

# --- Voice & Agent Settings -------------------------------------------------
JARVIS_AGENT_ENABLED = env_bool("JARVIS_AGENT_ENABLED", True)
JARVIS_CONVERSATION_MODE = env_bool("JARVIS_CONVERSATION_MODE", False)
JARVIS_WAKE_PROMPT = env_str("JARVIS_WAKE_PROMPT", "Yes, sir. 무엇을 도와드릴까요?")
JARVIS_STT_FAILURE_PROMPT = env_str(
    "JARVIS_STT_FAILURE_PROMPT", "음성 명령을 인식하지 못했습니다."
)
JARVIS_CHROME_URL = env_str("JARVIS_CHROME_URL", "https://www.google.com")
JARVIS_YOUTUBE_URL = env_str("JARVIS_YOUTUBE_URL", "https://www.youtube.com")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("jarvis")

_voice_session_active = threading.Event()
_voice_session_lock = threading.Lock()


# --- Audio & Mic Helpers ----------------------------------------------------

def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _resolve_input_device_index(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        idx = int(spec)
        sd.query_devices(idx)
        return idx
    needle = spec.lower()
    for idx, dev in _input_devices():
        if needle in dev["name"].lower():
            return idx
    raise ValueError(f"No input device matches {spec!r}")


def _probe_input_max_rms(device: int, blocksize: int) -> float | None:
    try:
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            peak = 0.0
            deadline = time.monotonic() + INPUT_PROBE_S
            while time.monotonic() < deadline:
                data, _ = stream.read(blocksize)
                peak = max(peak, rms_mono(data))
            return peak
    except sd.PortAudioError:
        return None


def _choose_input_device(blocksize: int) -> int:
    override = env_str("JARVIS_INPUT_DEVICE")
    if override:
        try:
            idx = _resolve_input_device_index(override)
        except ValueError as e:
            log.error("%s", e)
            log.error("Set JARVIS_INPUT_DEVICE to a device index or name substring.")
            raise SystemExit(1) from e
        name = sd.query_devices(idx)["name"]
        peak = _probe_input_max_rms(idx, blocksize)
        log.info("Using JARVIS_INPUT_DEVICE [%d]: %s", idx, name)
        if peak is None:
            log.warning("Could not open configured mic; trying anyway.")
        elif peak < INPUT_SILENT_RMS:
            log.warning(
                "Configured mic looks silent (probe rms=%.5f). Check input level.",
                peak,
            )
        else:
            log.info("Mic probe OK (rms=%.5f).", peak)
        return idx

    default = sd.default.device[0]
    if default is not None and default >= 0:
        default_name = sd.query_devices(default)["name"]
        peak = _probe_input_max_rms(default, blocksize)
        if peak is not None and peak >= INPUT_SILENT_RMS:
            log.info(
                "Using default microphone [%d]: %s (probe rms=%.5f)",
                default,
                default_name,
                peak,
            )
            return default
        log.warning(
            "Default mic [%d] %s is silent/unavailable (probe rms=%s); scanning...",
            default,
            default_name,
            f"{peak:.5f}" if peak is not None else "unopenable",
        )

    best_idx: int | None = None
    best_peak = -1.0
    for idx, dev in _input_devices():
        if default is not None and idx == default:
            continue
        peak = _probe_input_max_rms(idx, blocksize)
        if peak is not None and peak > best_peak:
            best_peak = peak
            best_idx = idx

    if best_idx is not None and best_peak >= INPUT_SILENT_RMS:
        log.info(
            "Auto-selected microphone [%d]: %s (probe rms=%.5f)",
            best_idx,
            sd.query_devices(best_idx)["name"],
            best_peak,
        )
        return best_idx

    if default is not None and default >= 0:
        log.warning("No active mic found; falling back to default [%d].", default)
        return default
    inputs = _input_devices()
    if not inputs:
        log.error("No input devices found.")
        raise SystemExit(1)
    idx, dev = inputs[0]
    log.warning("No active mic found; falling back to [%d] %s.", idx, dev["name"])
    return idx


# --- System & Application Automation Helpers --------------------------------

def play_song(uri: str | None = None) -> None:
    u = (uri or SONG_URI).strip()
    if not u:
        return
    try:
        if sys.platform == "win32":
            os.startfile(u)
        else:
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open SONG_URI: %s", e)


def _chrome_executable() -> str | None:
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p):
                return p
    elif sys.platform == "darwin":
        app_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(app_path):
            return app_path
    return shutil.which("google-chrome") or shutil.which("chrome")


def _open_url_in_chrome(url: str, *, new_window: bool = True, label: str = "URL") -> None:
    u = url.strip()
    if not u:
        return
    chrome = _chrome_executable()
    try:
        if chrome:
            args = [chrome]
            if new_window:
                args.append("--new-window")
            args.append(u)
            popen_kw: dict = {
                "args": args,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.Popen(**popen_kw)
        else:
            log.warning("Chrome not found; opening %s in default browser.", label)
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open %s in Chrome: %s", label, e)


def open_claude_in_chrome() -> None:
    url = env_str("CLAUDE_CODE_URL", "https://claude.ai/new")
    _open_url_in_chrome(url, new_window=True, label="Claude")


def open_binance_btc_in_chrome() -> None:
    url = env_str("BINANCE_BTC_URL", "https://www.binance.com/en/trade/BTC_USDT")
    _open_url_in_chrome(url, new_window=True, label="Binance BTC")


def _cursor_executable() -> str | None:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        for sub in ("Programs\\cursor\\Cursor.exe", "Programs\\Cursor\\Cursor.exe"):
            if local:
                p = os.path.join(local, *sub.split("\\"))
                if os.path.isfile(p):
                    return p
    elif sys.platform == "darwin":
        app_path = "/Applications/Cursor.app/Contents/MacOS/Cursor"
        if os.path.isfile(app_path):
            return app_path
    return shutil.which("cursor")


def open_cursor_window() -> None:
    exe = _cursor_executable()
    if not exe:
        log.warning("Could not find Cursor (install app or add to PATH).")
        return
    popen_kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        if OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
            subprocess.Popen([exe, "-n"], **popen_kw)
        else:
            subprocess.Popen([exe], **popen_kw)
    except OSError as e:
        log.warning("Could not start Cursor: %s", e)


def run_legacy_double_clap_actions() -> None:
    """Legacy double-clap routine: Spotify + Chrome windows + Cursor."""
    play_song(SONG_URI)
    open_claude_in_chrome()
    open_binance_btc_in_chrome()
    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():
        delay = max(0.0, JARVIS_AFTER_SONG_DELAY_S)
        if delay:
            time.sleep(delay)
        speak_text(JARVIS_WELCOME_PHRASE)
    open_cursor_window()


# --- Command Routing Layer --------------------------------------------------

def is_local_command(command: str) -> bool:
    """
    Check if the command is a fast local automation request rather than
    a complex AI task.
    """
    c = command.lower().strip()
    local_patterns = [
        r"크롬",
        r"chrome",
        r"브라우저",
        r"스포티파이",
        r"spotify",
        r"음악",
        r"노래",
        r"유튜브",
        r"youtube",
        r"커서",
        r"cursor",
        r"클로드",
        r"claude",
        r"바이낸스",
        r"binance",
    ]
    actions = [r"열어", r"켜", r"실행", r"틀어", r"시작", r"open", r"play", r"start"]
    has_target = any(re.search(pat, c) for pat in local_patterns)
    has_action = any(re.search(act, c) for act in actions)
    return has_target and has_action


def execute_local_command(command: str) -> str:
    """
    Execute quick local desktop automations directly in Jarvis.
    """
    c = command.lower().strip()
    if "스포티파이" in c or "spotify" in c or "노래" in c or "음악" in c:
        play_song(SONG_URI)
        return "스포티파이를 실행하고 음악을 재생합니다."
    if "유튜브" in c or "youtube" in c:
        _open_url_in_chrome(JARVIS_YOUTUBE_URL, new_window=True, label="YouTube")
        return "유튜브를 열었습니다."
    if "클로드" in c or "claude" in c:
        open_claude_in_chrome()
        return "Claude를 열었습니다."
    if "바이낸스" in c or "binance" in c:
        open_binance_btc_in_chrome()
        return "바이낸스 거래소를 열었습니다."
    if "커서" in c or "cursor" in c:
        open_cursor_window()
        return "Cursor 에디터를 열었습니다."
    if "크롬" in c or "chrome" in c or "브라우저" in c:
        _open_url_in_chrome(JARVIS_CHROME_URL, new_window=True, label="Chrome")
        return "크롬을 열었습니다."
    return "해당 로컬 명령을 실행했습니다."


def handle_command(command: str) -> str:
    """
    Route command to either local handlers or the OpenClaw AI Agent.
    """
    command = command.strip()
    if not command:
        return JARVIS_STT_FAILURE_PROMPT

    if is_local_command(command):
        log.info("Executing local desktop command: %s", command)
        return execute_local_command(command)

    log.info("Routing command to OpenClaw: %s", command)
    return ask_openclaw(command)


# --- Voice Interaction Pipeline ---------------------------------------------

def is_exit_phrase(phrase: str) -> bool:
    c = phrase.lower().strip()
    exit_words = {"종료", "끝", "그만", "stop", "bye", "exit", "quit", "취소", "닫아줘", "안녕히"}
    return any(w in c for w in exit_words)


def handle_voice_interaction() -> None:
    """
    Wake-up -> STT -> Command Routing -> TTS -> Optional Multi-turn Loop.
    """
    if not _voice_session_lock.acquire(blocking=False):
        log.info("Voice session already active; ignoring new trigger.")
        return

    _voice_session_active.set()
    try:
        # 1. Wake-up greeting
        speak_text(JARVIS_WAKE_PROMPT)

        conversation_mode = JARVIS_CONVERSATION_MODE
        while True:
            # 2. Listen & Transcribe
            command_text = listen_and_transcribe()
            if not command_text:
                if not conversation_mode:
                    speak_text(JARVIS_STT_FAILURE_PROMPT)
                break

            if is_exit_phrase(command_text):
                speak_text("네, 필요하시면 언제든 박수를 두 번 쳐주세요.")
                break

            # 3. Route and execute command
            response_text = handle_command(command_text)

            # 4. Speak response via TTS
            speak_text(response_text)

            if not conversation_mode:
                break

            time.sleep(0.3)

    except Exception as exc:
        log.error("Voice interaction error: %s", exc)
    finally:
        _voice_session_active.clear()
        _voice_session_lock.release()
        log.info("Voice session completed. Returning to clap listening mode.")


# --- Main Double-Clap Listener ----------------------------------------------

def main() -> int:
    blocksize = block_samples()
    noise_floor = 1e-4
    last_logged_double = 0.0
    first_clap_time: float | None = None
    spike_armed = True

    print("\n" + "=" * 60)
    print(" 🤖 JARVIS × OpenClaw Voice Assistant Activated")
    print("=" * 60)
    print(f" • Double Clap Trigger: 👏  👏  (Gap: {MIN_DOUBLE_GAP_S}s ~ {MAX_DOUBLE_GAP_S}s)")
    print(f" • Wake-up Prompt: \"{JARVIS_WAKE_PROMPT}\"")
    print(f" • Conversation Mode: {'ON' if JARVIS_CONVERSATION_MODE else 'OFF'}")
    print(f" • STT Provider: {env_str('JARVIS_STT_PROVIDER', 'speech_recognition / google')}")
    print(f" • OpenClaw Agent: {env_str('OPENCLAW_AGENT', 'main')}")
    print("=" * 60 + "\n")

    input_idx = _choose_input_device(blocksize)

    try:
        with sd.InputStream(
            device=input_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            while True:
                # If currently interacting via voice, pause clap detection
                if _voice_session_active.is_set():
                    time.sleep(0.1)
                    first_clap_time = None
                    continue

                data, overflowed = stream.read(blocksize)
                if overflowed:
                    log.warning("Input overflow; try a larger BLOCK_MS")

                level = rms_mono(data)

                quiet_gate = noise_floor * QUIET_GATE_MULT
                if level < quiet_gate:
                    noise_floor = NOISE_FLOOR_ALPHA * noise_floor + (
                        1.0 - NOISE_FLOOR_ALPHA
                    ) * level
                    noise_floor = max(noise_floor, 1e-7)

                threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
                now = time.monotonic()
                retrigger_level = threshold * RETRIGGER_RATIO

                if level < retrigger_level:
                    spike_armed = True

                if (
                    spike_armed
                    and level >= threshold
                    and (now - last_logged_double) >= COOLDOWN_S
                ):
                    spike_armed = False
                    if first_clap_time is None:
                        first_clap_time = now
                    else:
                        gap = now - first_clap_time
                        if gap < MIN_DOUBLE_GAP_S:
                            pass
                        elif gap <= MAX_DOUBLE_GAP_S:
                            first_clap_time = None
                            last_logged_double = now
                            print("\n👏 👏 [Double Clap Detected!] Activating Jarvis...")
                            threading.Thread(
                                target=handle_voice_interaction, daemon=True
                            ).start()
                        else:
                            first_clap_time = now

    except KeyboardInterrupt:
        log.info("Jarvis shutting down. Goodbye, sir.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
