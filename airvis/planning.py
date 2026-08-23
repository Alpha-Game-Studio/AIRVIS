"""Deprecated module kept for backward compatibility.

The canonical task model and planner now live in
:mod:`airvis.orchestration.task` and :mod:`airvis.orchestration.planner`.
"""

from __future__ import annotations

from .compat import deprecated
from .orchestration.planner import Planner as _Planner
from .orchestration.task import Plan, RetryPolicy, Task, TaskStatus, ToolStep

__all__ = ["Plan", "Planner", "RetryPolicy", "Task", "TaskStatus", "ToolStep"]


class Planner:
    """V4 synchronous planner shim over :class:`airvis.orchestration.Planner`."""

    def __init__(self) -> None:
        deprecated("airvis.planning.Planner", "airvis.orchestration.Planner")
        self._planner = _Planner()

    def create(self, prompt: str) -> Task:
        return Task(description=prompt.strip())

    def plan(self, prompt: str) -> list[Task]:
        from .core.asyncutil import run_blocking

        return run_blocking(self._planner.plan(prompt)).tasks
