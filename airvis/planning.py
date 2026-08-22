from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import uuid


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    prompt: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = TaskStatus.QUEUED
    dependencies: list[str] = field(default_factory=list)
    retry_count: int = 0


class Planner:
    def create(self, prompt: str) -> Task:
        return Task(prompt=prompt.strip())

    def plan(self, prompt: str) -> list[Task]:
        return [self.create(prompt)]
