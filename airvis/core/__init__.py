"""Cross-cutting primitives shared by every AIRVIS subsystem."""

from .config import AirvisConfig, RoutingStrategy
from .errors import AirvisError
from .events import Event, EventBus, EventType
from .health import HealthRegistry, HealthState, HealthStatus

__all__ = [
    "AirvisConfig",
    "AirvisError",
    "Event",
    "EventBus",
    "EventType",
    "HealthRegistry",
    "HealthState",
    "HealthStatus",
    "RoutingStrategy",
]
