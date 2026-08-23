"""Provider layer: decides which model actually generates a result."""

from .base import (
    GenerationRequest,
    GenerationResult,
    Message,
    Provider,
    ProviderCapabilities,
    ToolCall,
    Usage,
)
from .factory import (
    build_provider,
    build_provider_registry,
    discover_provider_ids,
    provider_from_environment,
)
from .http import AnthropicProvider, GeminiProvider, OpenAICompatibleProvider
from .mock import MockProvider, ScriptedProvider
from .registry import ProviderRegistry

__all__ = [
    "AnthropicProvider",
    "GeminiProvider",
    "GenerationRequest",
    "GenerationResult",
    "Message",
    "MockProvider",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderCapabilities",
    "ProviderRegistry",
    "ScriptedProvider",
    "ToolCall",
    "Usage",
    "build_provider",
    "build_provider_registry",
    "discover_provider_ids",
    "provider_from_environment",
]
