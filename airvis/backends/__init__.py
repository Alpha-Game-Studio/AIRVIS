"""Backend layer: decides how an agent executes."""

from .base import Backend, BackendType, ExecutionRequest, ExecutionResult
from .cli import CLIBackend, HermesBackend, OpenClawBackend, find_binary
from .factory import build_backend_registry
from .native import MCPBackend, NativeBackend
from .registry import BackendRegistry, BackendRouter

__all__ = [
    "Backend",
    "BackendRegistry",
    "BackendRouter",
    "BackendType",
    "CLIBackend",
    "ExecutionRequest",
    "ExecutionResult",
    "HermesBackend",
    "MCPBackend",
    "NativeBackend",
    "OpenClawBackend",
    "build_backend_registry",
    "find_binary",
]
