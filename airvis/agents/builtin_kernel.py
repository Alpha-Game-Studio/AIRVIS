"""Built-in specialist agent definitions used by the Agent Kernel."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Specialist:
    name: str
    capabilities: tuple[str, ...]
    system_role: str


SPECIALISTS = (
    Specialist("architect", ("architecture", "planning", "design"), "Design a minimal, testable implementation plan."),
    Specialist("researcher", ("research", "web", "analysis"), "Gather evidence and summarize only actionable findings."),
    Specialist("coder", ("coding", "implementation", "refactor"), "Implement the requested change with production-quality code."),
    Specialist("debugger", ("debugging", "diagnostics", "repair"), "Find the root cause, reproduce it, and implement the smallest robust fix."),
    Specialist("tester", ("testing", "verification", "qa"), "Verify behavior with focused tests and report failures precisely."),
    Specialist("reviewer", ("review", "security", "quality"), "Review the result adversarially for correctness, security and regressions."),
)


def choose_specialist(capability: str) -> Specialist:
    needle = capability.lower().strip()
    for specialist in SPECIALISTS:
        if needle in specialist.capabilities or any(needle in item for item in specialist.capabilities):
            return specialist
    return SPECIALISTS[0]


__all__ = ["SPECIALISTS", "Specialist", "choose_specialist"]
