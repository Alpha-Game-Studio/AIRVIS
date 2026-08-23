"""Local deterministic providers.

``MockProvider`` performs no network I/O: it synthesises an answer from the
conversation and any tool observations already present. That makes the whole
pipeline runnable — and testable — without API keys, while remaining a real
provider implementation rather than a stub that pretends work happened.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from ..core.health import HealthState, HealthStatus
from .base import GenerationRequest, GenerationResult, Provider, ProviderCapabilities, Usage

MAX_OBSERVATION_CHARS = 1500


class MockProvider(Provider):
    """Deterministic offline provider used as the default and as a fallback."""

    id = "mock"
    capabilities = ProviderCapabilities(chat=True, streaming=True, tool_calling=False, structured_output=True)
    default_model = "airvis-local"
    models = ("airvis-local",)
    local = True
    quality = 0.35

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        started = time.perf_counter()
        prompt = request.last_user_message.strip()
        observations = [message for message in request.messages if message.role == "tool"]
        text = _summarise(prompt, observations)
        usage = Usage(
            input_tokens=await self.count_tokens(request),
            output_tokens=max(1, len(text) // 4),
        )
        return GenerationResult(
            text=text,
            provider=self.id,
            model=self.resolve_model(request.model),
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        result = await self.generate(request)
        for index in range(0, len(result.text), 48):
            yield result.text[index : index + 48]
            await asyncio.sleep(0)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "local provider is always available", time.time(), 0.0)


class ScriptedProvider(Provider):
    """Replays a fixed list of responses; used by tests and dry runs."""

    id = "scripted"
    capabilities = ProviderCapabilities(chat=True, structured_output=True)
    default_model = "scripted"
    local = True
    quality = 0.3

    def __init__(self, responses: list[str] | None = None, **overrides: Any) -> None:
        super().__init__(**overrides)
        self.responses = list(responses or [])
        self._index = 0

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if self._index < len(self.responses):
            text = self.responses[self._index]
            self._index += 1
        else:
            text = self.responses[-1] if self.responses else ""
        return GenerationResult(text=text, provider=self.id, model=self.resolve_model(request.model))

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.HEALTHY, "scripted", time.time(), 0.0)


def _summarise(prompt: str, observations: list[Any]) -> str:
    if not observations:
        return f"Mock Provider 응답: {prompt}" if prompt else "Mock Provider 응답: (빈 요청)"
    lines = [f"Mock Provider 응답: {prompt}" if prompt else "Mock Provider 응답:"]
    lines.append(f"수집한 도구 관찰 {len(observations)}건을 종합했습니다.")
    for observation in observations[-6:]:
        lines.append(f"- {_render(observation.content)}")
    return "\n".join(lines)


def _render(content: str) -> str:
    text = content.strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text[:MAX_OBSERVATION_CHARS]
    if isinstance(payload, dict):
        summary = payload.get("summary") or payload.get("output") or payload
        return str(summary)[:MAX_OBSERVATION_CHARS]
    return str(payload)[:MAX_OBSERVATION_CHARS]


__all__ = ["MockProvider", "ScriptedProvider"]
