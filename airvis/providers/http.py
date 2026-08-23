"""Network-backed providers built on the standard library only.

Blocking HTTP is executed on a worker thread so the async pipeline is never
stalled. Errors are translated into the structured provider exceptions the
repair system classifies on.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from typing import Any

from ..core.errors import ProviderError, ProviderTimeoutError, ProviderUnavailableError, RateLimitError
from ..core.health import HealthState, HealthStatus
from .base import (
    GenerationRequest,
    GenerationResult,
    Provider,
    ProviderCapabilities,
    ToolCall,
    Usage,
    tool_schemas_to_openai,
)


def _post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float, provider_id: str) -> Any:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code == 429:
            raise RateLimitError(f"{provider_id} rate limited: {detail}", provider=provider_id, status=429) from exc
        if exc.code in {401, 403}:
            raise ProviderUnavailableError(
                f"{provider_id} rejected the credentials ({exc.code})", provider=provider_id, status=exc.code
            ) from exc
        if exc.code >= 500:
            raise ProviderUnavailableError(
                f"{provider_id} server error {exc.code}: {detail}", provider=provider_id, status=exc.code
            ) from exc
        raise ProviderError(f"{provider_id} HTTP {exc.code}: {detail}", provider=provider_id, status=exc.code) from exc
    except TimeoutError as exc:
        raise ProviderTimeoutError(f"{provider_id} timed out after {timeout}s", provider=provider_id) from exc
    except urllib.error.URLError as exc:
        raise ProviderUnavailableError(f"{provider_id} is unreachable: {exc.reason}", provider=provider_id) from exc
    except ValueError as exc:
        raise ProviderError(f"{provider_id} returned invalid JSON: {exc}", provider=provider_id) from exc


class OpenAICompatibleProvider(Provider):
    """Covers OpenAI, xAI, OpenRouter, Ollama and any compatible gateway."""

    capabilities = ProviderCapabilities(
        chat=True, streaming=True, tool_calling=True, structured_output=True, vision=True
    )

    def __init__(
        self,
        id: str,
        base_url: str,
        api_key: str = "",
        model: str = "",
        *,
        timeout: float = 60.0,
        local: bool = False,
        quality: float = 0.6,
        cost_per_million_input: float = 0.0,
        cost_per_million_output: float = 0.0,
        extra_headers: dict[str, str] | None = None,
        **overrides: Any,
    ) -> None:
        self.id = id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = model
        self.timeout = timeout
        self.local = local
        self.quality = quality
        self.cost_per_million_input = cost_per_million_input
        self.cost_per_million_output = cost_per_million_output
        self.extra_headers = dict(extra_headers or {})
        super().__init__(**overrides)

    # -- internals -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, request: GenerationRequest, *, stream: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.resolve_model(request.model),
            "messages": [message.to_dict() for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            payload["tools"] = tool_schemas_to_openai(request.tools)
        if request.stop:
            payload["stop"] = request.stop
        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        if stream:
            payload["stream"] = True
        return payload

    # -- API -------------------------------------------------------------------

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        started = time.perf_counter()
        data = await asyncio.to_thread(
            _post,
            f"{self.base_url}/chat/completions",
            self._payload(request),
            self._headers(),
            request.timeout or self.timeout,
            self.id,
        )
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.id} returned no choices", provider=self.id)
        message = choices[0].get("message") or {}
        usage = data.get("usage") or {}
        return GenerationResult(
            text=str(message.get("content") or "").strip(),
            provider=self.id,
            model=str(data.get("model") or self.resolve_model(request.model)),
            tool_calls=_parse_openai_tool_calls(message.get("tool_calls")),
            usage=Usage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))),
            finish_reason=str(choices[0].get("finish_reason") or "stop"),
            latency_ms=(time.perf_counter() - started) * 1000,
            raw=data,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _consume() -> None:
            body = json.dumps(self._payload(request, stream=True)).encode("utf-8")
            http_request = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body, headers=self._headers(), method="POST"
            )
            try:
                with urllib.request.urlopen(http_request, timeout=request.timeout or self.timeout) as response:
                    for raw_line in response:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            payload = json.loads(chunk)
                        except ValueError:
                            continue
                        for choice in payload.get("choices", []):
                            piece = (choice.get("delta") or {}).get("content")
                            if piece:
                                loop.call_soon_threadsafe(queue.put_nowait, piece)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = asyncio.create_task(asyncio.to_thread(_consume))
        try:
            while True:
                piece = await queue.get()
                if piece is None:
                    break
                yield piece
        finally:
            await task

    async def health_check(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            await asyncio.to_thread(_probe_models, f"{self.base_url}/models", self._headers(), 10.0, self.id)
        except ProviderError as exc:
            return HealthStatus(HealthState.UNHEALTHY, str(exc), time.time())
        latency = (time.perf_counter() - started) * 1000
        return HealthStatus(HealthState.HEALTHY, "models endpoint reachable", time.time(), latency)


class AnthropicProvider(Provider):
    """Anthropic Messages API."""

    id = "anthropic"
    capabilities = ProviderCapabilities(chat=True, tool_calling=True, vision=True, reasoning=True)
    default_model = "claude-sonnet-5"
    quality = 0.92
    cost_per_million_input = 3.0
    cost_per_million_output = 15.0

    def __init__(self, api_key: str = "", model: str = "", *, timeout: float = 60.0, **overrides: Any) -> None:
        self.api_key = api_key
        if model:
            self.default_model = model
        self.timeout = timeout
        super().__init__(**overrides)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        started = time.perf_counter()
        system = "\n\n".join(item.content for item in request.messages if item.role == "system")
        conversation = [
            {"role": "assistant" if item.role == "assistant" else "user", "content": item.content}
            for item in request.messages
            if item.role in {"user", "assistant", "tool"}
        ]
        payload: dict[str, Any] = {
            "model": self.resolve_model(request.model),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": conversation or [{"role": "user", "content": request.last_user_message}],
        }
        if system:
            payload["system"] = system
        data = await asyncio.to_thread(
            _post,
            "https://api.anthropic.com/v1/messages",
            payload,
            {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            request.timeout or self.timeout,
            self.id,
        )
        blocks = data.get("content") or []
        text = "".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text").strip()
        usage = data.get("usage") or {}
        return GenerationResult(
            text=text,
            provider=self.id,
            model=str(data.get("model") or self.resolve_model(request.model)),
            usage=Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))),
            finish_reason=str(data.get("stop_reason") or "stop"),
            latency_ms=(time.perf_counter() - started) * 1000,
            raw=data,
        )

    async def health_check(self) -> HealthStatus:
        if not self.api_key:
            return HealthStatus(HealthState.UNHEALTHY, "ANTHROPIC_API_KEY is not set", time.time())
        return HealthStatus(HealthState.UNKNOWN, "credentials present, not probed", time.time())


class GeminiProvider(Provider):
    """Google Gemini generateContent API."""

    id = "gemini"
    capabilities = ProviderCapabilities(chat=True, vision=True, structured_output=True)
    default_model = "gemini-2.0-flash"
    quality = 0.8
    cost_per_million_input = 0.1
    cost_per_million_output = 0.4

    def __init__(self, api_key: str = "", model: str = "", *, timeout: float = 60.0, **overrides: Any) -> None:
        self.api_key = api_key
        if model:
            self.default_model = model
        self.timeout = timeout
        super().__init__(**overrides)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        started = time.perf_counter()
        contents = [
            {"role": "model" if item.role == "assistant" else "user", "parts": [{"text": item.content}]}
            for item in request.messages
            if item.role != "system"
        ]
        payload: dict[str, Any] = {
            "contents": contents or [{"role": "user", "parts": [{"text": request.last_user_message}]}],
            "generationConfig": {"temperature": request.temperature, "maxOutputTokens": request.max_tokens},
        }
        system = "\n\n".join(item.content for item in request.messages if item.role == "system")
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        model = self.resolve_model(request.model)
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            f"?key={urllib.parse.quote(self.api_key)}"
        )
        data = await asyncio.to_thread(
            _post, url, payload, {"Content-Type": "application/json"}, request.timeout or self.timeout, self.id
        )
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderError(f"{self.id} returned no candidates", provider=self.id)
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text", "")) for part in parts).strip()
        usage = data.get("usageMetadata") or {}
        return GenerationResult(
            text=text,
            provider=self.id,
            model=model,
            usage=Usage(int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0))),
            finish_reason=str(candidates[0].get("finishReason") or "stop"),
            latency_ms=(time.perf_counter() - started) * 1000,
            raw=data,
        )

    async def health_check(self) -> HealthStatus:
        if not self.api_key:
            return HealthStatus(HealthState.UNHEALTHY, "GOOGLE_API_KEY is not set", time.time())
        return HealthStatus(HealthState.UNKNOWN, "credentials present, not probed", time.time())


def _probe_models(url: str, headers: dict[str, str], timeout: float, provider_id: str) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ProviderUnavailableError(f"{provider_id} rejected the credentials", provider=provider_id) from exc
        raise ProviderError(f"{provider_id} HTTP {exc.code}", provider=provider_id) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ProviderUnavailableError(f"{provider_id} is unreachable: {exc}", provider=provider_id) from exc


def _parse_openai_tool_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in raw or []:
        function = item.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except ValueError:
                arguments = {}
        calls.append(ToolCall(name=name, arguments=arguments if isinstance(arguments, dict) else {},
                              id=str(item.get("id") or "")[:32] or ToolCall(name=name).id))
    return calls


__all__ = ["AnthropicProvider", "GeminiProvider", "OpenAICompatibleProvider"]
