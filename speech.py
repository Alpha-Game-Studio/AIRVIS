from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from config import BASE_DIR, env_bool, env_float, env_str


log = logging.getLogger("speech")


# --- Text Preprocessing for Natural Speech ----------------------------------

def clean_text_for_speech(text: str) -> str:
    """Clean markdown links, URLs, and formatting so TTS speaks naturally."""
    # Replace markdown links [Title](URL) with Title
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # Remove raw URLs
    text = re.sub(r"https?://\S+", "", text)
    # Remove markdown symbols (*, _, #, `, ~, >)
    text = re.sub(r"[*_#`~>]", "", text)
    # Clean whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


# --- ElevenLabs TTS Helpers -------------------------------------------------

def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = env_str("ELEVENLABS_PCM_SAMPLE_RATE")
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    """voice_id, model_id, output_format, pcm_sample_rate."""
    voice = env_str("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
    model = env_str("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
    fmt = env_str("ELEVENLABS_OUTPUT_FORMAT", "pcm_24000")
    rate = _elevenlabs_pcm_sample_rate(fmt)
    return voice, model, fmt, rate


def _tts_cache_dir() -> Path:
    override = env_str("JARVIS_TTS_CACHE_DIR") or env_str("JARVIS_WELCOME_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return BASE_DIR / ".cache" / "jarvis_tts"


def _tts_cache_path(text: str, voice_id: str, model_id: str, output_format: str) -> Path:
    key = f"{text}|{voice_id}|{model_id}|{output_format}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _tts_cache_dir() / f"{digest}.wav"


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            if ch != 1 or sw != 2:
                log.warning("Unsupported cached WAV (channels=%s, width=%s).", ch, sw)
                return False
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as exc:
        log.warning("Could not read cached TTS audio: %s", exc)
        return False
    if not raw:
        return False
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    try:
        sd.play(pcm_f, rate)
        sd.wait()
        return True
    except Exception as exc:
        log.warning("Audio playback error: %s", exc)
        return False


def _save_pcm_wav_file(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


def _system_tts_fallback(text: str) -> bool:
    """Fallback to native operating system TTS."""
    clean = clean_text_for_speech(text)
    if not clean:
        return True

    try:
        if sys.platform == "darwin":
            preferred_voice = env_str("JARVIS_MACOS_VOICE", "Yuna")
            cmd = ["say"]
            if preferred_voice:
                cmd.extend(["-v", preferred_voice])
            cmd.append(clean)
            subprocess.run(cmd, check=False)
            return True
        elif sys.platform == "win32":
            clean_text = clean.replace('"', '""').replace("`", "``")
            ps_script = f'Add-Type -AssemblyName System.Speech; $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; $synth.Speak("{clean_text}");'
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=False)
            return True
        elif sys.platform.startswith("linux"):
            if shutil.which("spd-say"):
                subprocess.run(["spd-say", clean], check=False)
                return True
            elif shutil.which("espeak"):
                subprocess.run(["espeak", "-v", "ko", clean], check=False)
                return True
    except Exception as exc:
        log.warning("System TTS fallback error: %s", exc)
    return False


def speak_text(text: str) -> bool:
    """
    Speak text cleanly using ElevenLabs TTS with local caching.
    """
    raw_text = text.strip()
    if not raw_text:
        return False

    print(f"\n[Jarvis] 🗣️  {raw_text}\n")

    # Clean markdown and URLs for natural speaking
    spoken_text = clean_text_for_speech(raw_text)
    if not spoken_text:
        spoken_text = raw_text

    voice_id, model_id, output_format, pcm_rate = elevenlabs_env_config()
    cache_enabled = env_bool("JARVIS_TTS_CACHE_ENABLED", True)
    cache_path = _tts_cache_path(spoken_text, voice_id, model_id, output_format)

    # 1. Play from local cache if present
    if cache_enabled and cache_path.is_file():
        if _play_pcm_wav_file(cache_path):
            return True

    # 2. Try ElevenLabs API
    api_key = env_str("ELEVENLABS_API_KEY")
    elevenlabs_success = False

    if api_key and voice_id:
        try:
            from elevenlabs.client import ElevenLabs

            client = ElevenLabs(api_key=api_key)
            chunks = client.text_to_speech.convert(
                voice_id=voice_id,
                text=spoken_text,
                model_id=model_id,
                output_format=output_format,
            )
            raw = b"".join(chunks)
            if raw:
                if cache_enabled:
                    try:
                        _save_pcm_wav_file(cache_path, raw, pcm_rate)
                    except OSError as exc:
                        log.warning("Could not save TTS cache: %s", exc)
                pcm_i16 = np.frombuffer(raw, dtype=np.int16)
                pcm_f = pcm_i16.astype(np.float32) / 32768.0
                sd.play(pcm_f, pcm_rate)
                sd.wait()
                return True
        except Exception as exc:
            log.warning(
                "ElevenLabs TTS failed (%s). Falling back to native system voice...", exc
            )

    # 3. Fallback to native system voice if ElevenLabs fails or is unconfigured
    if not elevenlabs_success:
        return _system_tts_fallback(spoken_text)

    return True


# --- Real-Time VAD (Voice Activity Detection) STT --------------------------

def _resolve_input_device_index() -> int | None:
    override = env_str("JARVIS_INPUT_DEVICE")
    if not override:
        return None
    if override.isdigit():
        return int(override)
    needle = override.lower()
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] >= 1 and needle in dev["name"].lower():
            return i
    return None


def _record_vad_wav(path: Path) -> bool:
    """
    Real-time microphone stream with energy-based Voice Activity Detection (VAD).
    Immediately stops recording when the user finishes speaking (silence detected).
    """
    sample_rate = 16000
    chunk_samples = 512  # 32ms per chunk
    channels = 1
    input_device = _resolve_input_device_index()

    speech_threshold = env_float("JARVIS_VAD_THRESHOLD", 0.025)
    silence_limit_s = env_float("JARVIS_VAD_SILENCE_SECONDS", 0.6)
    max_record_s = env_float("JARVIS_VAD_MAX_SECONDS", 7.0)
    initial_timeout_s = env_float("JARVIS_VAD_TIMEOUT_SECONDS", 3.5)

    recorded_chunks: list[np.ndarray] = []
    has_spoken = False
    silence_start_time: float | None = None
    start_time = time.monotonic()

    print("[Jarvis] 🎙️  듣고 있습니다... (말씀해 주세요)")

    try:
        with sd.InputStream(
            device=input_device,
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            blocksize=chunk_samples,
        ) as stream:
            while True:
                chunk, overflowed = stream.read(chunk_samples)
                if overflowed:
                    pass

                rms = float(np.sqrt(np.mean(chunk**2))) if chunk.size else 0.0
                now = time.monotonic()
                elapsed = now - start_time

                # Check initial silence timeout
                if not has_spoken and elapsed > initial_timeout_s:
                    log.info("No speech detected within %.1fs timeout.", initial_timeout_s)
                    break

                # Check max recording safety limit
                if elapsed > max_record_s:
                    log.info("Reached maximum recording limit (%.1fs).", max_record_s)
                    break

                # Detect speech onset
                if rms >= speech_threshold:
                    if not has_spoken:
                        has_spoken = True
                        log.info("Speech started (rms=%.4f).", rms)
                    silence_start_time = None
                    recorded_chunks.append(chunk.copy())
                else:
                    if has_spoken:
                        recorded_chunks.append(chunk.copy())
                        if silence_start_time is None:
                            silence_start_time = now
                        elif (now - silence_start_time) >= silence_limit_s:
                            log.info(
                                "Silence detected (%.2fs) after speech. Stopping recording.",
                                silence_limit_s,
                            )
                            break

    except Exception as exc:
        log.warning("Audio recording error: %s", exc)
        return False

    if not recorded_chunks or not has_spoken:
        return False

    # Combine recorded audio chunks
    full_audio = np.concatenate(recorded_chunks, axis=0)
    audio_i16 = (np.clip(full_audio, -1.0, 1.0) * 32767.0).astype(np.int16)

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_i16.tobytes())

    return True


def _transcribe_with_speech_recognition(path: Path) -> str:
    try:
        import speech_recognition as sr
    except ImportError:
        log.warning("Install SpeechRecognition or choose another JARVIS_STT_PROVIDER.")
        return ""
    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = False
    language = env_str("JARVIS_STT_LANGUAGE", "ko-KR")
    try:
        with sr.AudioFile(str(path)) as source:
            audio = recognizer.record(source)
        transcription = recognizer.recognize_google(audio, language=language).strip()
        return transcription
    except sr.UnknownValueError:
        log.info("STT could not understand the audio.")
    except sr.RequestError as exc:
        log.warning("STT request failed: %s", exc)
    return ""


def _transcribe_with_openai(path: Path) -> str:
    api_key = env_str("OPENAI_API_KEY")
    if not api_key:
        log.warning("OPENAI_API_KEY is required when JARVIS_STT_PROVIDER=openai.")
        return ""
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("Install openai or choose another JARVIS_STT_PROVIDER.")
        return ""
    model = env_str("OPENAI_STT_MODEL", "whisper-1")
    try:
        client = OpenAI(api_key=api_key)
        with path.open("rb") as audio_file:
            result = client.audio.transcriptions.create(model=model, file=audio_file)
        return (getattr(result, "text", "") or "").strip()
    except Exception as exc:
        log.warning("OpenAI STT failed: %s", exc)
        return ""


def listen_and_transcribe() -> str:
    """
    Record with real-time VAD and return transcribed text.
    JARVIS_STT_PROVIDER can be `speech_recognition`, `openai`, or `none`.
    """
    provider = env_str("JARVIS_STT_PROVIDER")
    if not provider:
        provider = "openai" if env_str("OPENAI_API_KEY") else "speech_recognition"
    provider = provider.lower()
    if provider in {"none", "off", "disabled"}:
        log.warning("STT is disabled. Set JARVIS_STT_PROVIDER to enable it.")
        return ""

    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_name = tmp.name
        path = Path(tmp_name)

        has_audio = _record_vad_wav(path)
        if not has_audio:
            return ""

        if provider == "openai":
            text = _transcribe_with_openai(path)
        elif provider in {"speech_recognition", "google"}:
            text = _transcribe_with_speech_recognition(path)
        else:
            log.warning("Unsupported JARVIS_STT_PROVIDER: %s", provider)
            text = ""

        if text:
            print(f"[User] 🗣️  \"{text}\"")
        return text
    except Exception as exc:
        log.warning("STT failed: %s", exc)
        return ""
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
