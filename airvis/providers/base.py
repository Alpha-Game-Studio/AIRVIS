"""Provider interface: the layer that decides *which model generates text*."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import CapabilityError
from ..core.health import HealthState, HealthStatus


@dataclass(frozen=True)
class ProviderCapabilities:
    chat: bool = True
    streaming: bool = False
    tool_calling: bool = False
    vision: bool = False
    structured_output: bool = False
    reasoning: bool = False
    embeddings: bool = False

    def supports(self, name: str) -> bool:
        return bool(getattr(self, name, False))

    def names(self) -> set[str]:
        return {key for key, value in self.__dict__.items() if value}

    def to_list(self) -> list[str]:
        return sorted(self.names())


@dataclass
class Message:
    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload

    @classmethod
    def coerce(cls, value: Message | dict[str, Any]) -> Message:
        if isinstance(value, Message):
            return value
        return cls(role=str(value.get("role", "user")), content=str(value.get("content", "")),
                   name=value.get("name"), tool_call_id=value.get("tool_call_id"))


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "total": self.total}


@dataclass
class GenerationRequest:
    messages: list[Message]
    model: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    temperature: float = 0.2
    max_tokens: int = 2048
    stop: list[str] = field(default_factory=list)
    response_format: str = ""
    timeout: float = 60.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, messages: Iterable[Message | dict[str, Any]], **kwargs: Any) -> GenerationRequest:
        return cls(messages=[Message.coerce(item) for item in messages], **kwargs)

    @property
    def last_user_message(self) -> str:
        for message in reversed(self.messages):
            if message.role == "user":
                return message.content
        return self.messages[-1].content if self.messages else ""


@dataclass
class GenerationResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "provider": self.provider, "model": self.model,
                "tool_calls": [call.to_dict() for call in self.tool_calls],
                "usage": self.usage.to_dict(), "finish_reason": self.finish_reason,
                "latency_ms": round(self.latency_ms, 2)}


class Provider:
    id: str = ""
    capabilities: ProviderCapabilities = ProviderCapabilities()
    models: tuple[str, ...] = ()
    default_model: str = ""
    cost_per_million_input: float = 0.0
    cost_per_million_output: float = 0.0
    local: bool = False
    quality: float = 0.5

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            setattr(self, key, value)
        if not self.id:
            raise ValueError(f"{type(self).__name__} must define an id")

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        raise NotImplementedError(f"provider {self.id} does not implement generate()")

    def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        if not self.capabilities.streaming:
            raise CapabilityError(f"provider {self.id} does not support streaming", provider=self.id)
        raise NotImplementedError(f"provider {self.id} declares streaming but does not implement it")

    async def count_tokens(self, request: GenerationRequest | str) -> int:
        text = request if isinstance(request, str) else "\n".join(message.content for message in request.messages)
        return max(1, len(text) // 4)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.UNKNOWN, "no health check implemented", time.time())

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise CapabilityError(f"provider {self.id} does not support embeddings", provider=self.id)

    async def close(self) -> None:
        pass

    def supports_model(self, model: str) -> bool:
        return not self.models or not model or model in self.models

    def resolve_model(self, model: str = "") -> str:
        return model or self.default_model or (self.models[0] if self.models else "")

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "type": type(self).__name__, "capabilities": self.capabilities.to_list(),
                "models": list(self.models), "default_model": self.default_model, "local": self.local,
                "quality": self.quality, "cost_per_million_input": self.cost_per_million_input,
                "cost_per_million_output": self.cost_per_million_output}

    def __repr__(self) -> str:
        return f"<Provider {self.id} model={self.default_model!r}>"


def openai_tool_name(name: str) -> str:
    """Return an OpenAI-compatible function name while preserving tool identity.

    AIRVIS tool names intentionally use dotted namespaces (``filesystem.read``),
    but OpenAI-compatible APIs require ``^[a-zA-Z0-9_-]+$``. A reversible-looking
    prefix is not needed because the caller keeps a per-request mapping back to
    the canonical AIRVIS name.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(name))
    safe = re.sub(r"_+", "_", safe).strip("_") or "tool"
    if len(safe) > 64:
        safe = safe[:64]
    return safe


def tool_schemas_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert AIRVIS tool schemas into OpenAI function-calling format."""
    converted: list[dict[str, Any]] = []
    used: set[str] = set()
    for tool in tools:
        original = str(tool.get("name", ""))
        safe = openai_tool_name(original)
        base = safe
        suffix = 2
        while safe in used:
            suffix_text = f"_{suffix}"
            safe = (base[: 64 - len(suffix_text)] + suffix_text)
            suffix += 1
        used.add(safe)
        converted.append({"type": "function", "function": {
            "name": safe,
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}, "required": []},
        }})
    return converted


__all__ = ["GenerationRequest", "GenerationResult", "Message", "Provider", "ProviderCapabilities",
           "ToolCall", "Usage", "openai_tool_name", "tool_schemas_to_openai"]
