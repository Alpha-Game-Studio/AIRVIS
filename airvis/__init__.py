"""AIRVIS — AI orchestration engine."""

from .agents import AgentRegistry, AgentRouter, AgentSpec
from .core.config import AirvisConfig
from .core.errors import AirvisError
from .core.events import Event, EventBus, EventType
from .engine import AirvisEngine, build_engine
from .orchestration import (
    Orchestrator,
    Plan,
    Planner,
    ReviewResult,
    Task,
    TaskStatus,
    WorkflowResult,
    WorkflowStatus,
)
from .runtime import AgentRuntime, AgentState

__version__ = "6.0.0"

__all__ = [
    "AgentRegistry",
    "AgentRouter",
    "AgentRuntime",
    "AgentSpec",
    "AgentState",
    "AirvisConfig",
    "AirvisEngine",
    "AirvisError",
    "Event",
    "EventBus",
    "EventType",
    "Orchestrator",
    "Plan",
    "Planner",
    "ReviewResult",
    "Task",
    "TaskStatus",
    "WorkflowResult",
    "WorkflowStatus",
    "__version__",
    "build_engine",
]
