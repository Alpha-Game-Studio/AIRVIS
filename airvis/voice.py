from __future__ import annotations

from pathlib import Path
from typing import Protocol


class STTProvider(Protocol):
    id: str

    def transcribe(self, audio_path: Path) -> str: ...


class TTSProvider(Protocol):
    id: str

    def speak(self, text: str) -> bool: ...


class SpeechRecognitionSTT:
    id = "speech_recognition"

    def transcribe(self, audio_path: Path) -> str:
        from speech import _transcribe_with_speech_recognition
        return _transcribe_with_speech_recognition(audio_path)


class OpenAISTT:
    id = "openai"

    def transcribe(self, audio_path: Path) -> str:
        from speech import _transcribe_with_openai
        return _transcribe_with_openai(audio_path)


class ElevenLabsTTS:
    id = "elevenlabs"

    def speak(self, text: str) -> bool:
        from speech import speak_text
        return speak_text(text)


class SystemTTS:
    id = "system"

    def speak(self, text: str) -> bool:
        from speech import _system_tts_fallback
        return _system_tts_fallback(text)
