"""AIRVIS Agent Kernel: the autonomous control loop behind AgentOS.

This module is intentionally independent from any external agent product.
Agents can plan, execute tools, observe results, delegate work to child agents,
evaluate progress, recover from failure, and re-plan until a bounded goal is
verified or the safety budget is exhausted.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


class AgentExecutor(Protocol):
    async def __call__(self, request: str, *, strategy: str | None = None, **kwargs: Any) -> Any: ...


@dataclass
class AgentGoal:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    prompt: str = ""
    status: str = "pending"
    iteration: int = 0
    max_iterations: int = 20
    history: list[dict[str, Any]] = field(default_factory=list)
    result: Any = None


@dataclass
class AgentTask:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    goal_id: str = ""
    prompt: str = ""
    agent: str = "general"
    parent_id: str | None = None
    status: str = "pending"
    attempts: int = 0
    result: Any = None
    error: str | None = None


@dataclass
class KernelPolicy:
    max_iterations: int = 20
    max_parallel_agents: int = 4
    max_delegations: int = 16
    max_repairs: int = 8
    task_timeout: float = 300.0
    require_verification: bool = True


@dataclass
class KernelEvent:
    type: str
    goal_id: str
    task_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class AgentKernel:
    """Goal-directed supervisor that turns AIRVIS workflows into autonomous agents."""

    def __init__(
        self,
        executor: AgentExecutor,
        *,
        planner: Callable[[AgentGoal], Awaitable[list[AgentTask]]] | None = None,
        evaluator: Callable[[AgentGoal, list[AgentTask]], Awaitable[dict[str, Any]]] | None = None,
        policy: KernelPolicy | None = None,
        event_sink: Callable[[KernelEvent], Any] | None = None,
    ) -> None:
        self.executor = executor
        self.planner = planner
        self.evaluator = evaluator
        self.policy = policy or KernelPolicy()
        self.event_sink = event_sink
        self.tasks: dict[str, AgentTask] = {}
        self.goals: dict[str, AgentGoal] = {}
        self._delegations = 0
        self._repairs = 0

    async def _emit(self, event: KernelEvent) -> None:
        if self.event_sink:
            value = self.event_sink(event)
            if asyncio.iscoroutine(value):
                await value

    async def run(self, prompt: str, *, strategy: str | None = None, max_iterations: int | None = None) -> AgentGoal:
        goal = AgentGoal(prompt=prompt, max_iterations=max_iterations or self.policy.max_iterations)
        self.goals[goal.id] = goal
        await self._emit(KernelEvent("goal.started", goal.id, data={"prompt": prompt}))
        goal.status = "running"

        for _ in range(goal.max_iterations):
            goal.iteration += 1
            tasks = await self._plan(goal, strategy=strategy)
            if not tasks:
                evaluation = await self._evaluate(goal, [])
                if evaluation.get("verified") or not self.policy.require_verification:
                    goal.status, goal.result = "completed", evaluation.get("result")
                    break
                await self._repair(goal, "planner returned no executable tasks")
                continue

            results = await self._execute_tasks(goal, tasks, strategy=strategy)
            evaluation = await self._evaluate(goal, results)
            goal.history.append({"iteration": goal.iteration, "tasks": [t.id for t in tasks], "evaluation": evaluation})
            if evaluation.get("verified") or (evaluation.get("done") and not self.policy.require_verification):
                goal.status, goal.result = "completed", evaluation.get("result")
                break
            if evaluation.get("delegations"):
                await self._delegate(goal, evaluation["delegations"], strategy=strategy)
            else:
                await self._repair(goal, str(evaluation.get("reason", "goal not yet verified")))
        else:
            goal.status = "exhausted"

        if goal.status == "running":
            goal.status = "completed" if goal.result is not None else "exhausted"
        await self._emit(KernelEvent("goal.finished", goal.id, data={"status": goal.status, "iteration": goal.iteration}))
        return goal

    async def _plan(self, goal: AgentGoal, *, strategy: str | None) -> list[AgentTask]:
        if self.planner:
            planned = await self.planner(goal)
        else:
            planned = [AgentTask(goal_id=goal.id, prompt=goal.prompt, agent="general")]
        for task in planned:
            task.goal_id = goal.id
            self.tasks[task.id] = task
        await self._emit(KernelEvent("plan.created", goal.id, data={"tasks": [t.id for t in planned]}))
        return planned

    async def _execute_tasks(self, goal: AgentGoal, tasks: list[AgentTask], *, strategy: str | None) -> list[AgentTask]:
        semaphore = asyncio.Semaphore(self.policy.max_parallel_agents)

        async def execute(task: AgentTask) -> AgentTask:
            async with semaphore:
                task.status = "running"
                task.attempts += 1
                await self._emit(KernelEvent("task.started", goal.id, task.id, {"agent": task.agent, "prompt": task.prompt}))
                try:
                    task.result = await asyncio.wait_for(self.executor(task.prompt, strategy=strategy, agent=task.agent), self.policy.task_timeout)
                    task.status = "completed"
                except Exception as exc:
                    task.status, task.error = "failed", str(exc)
                await self._emit(KernelEvent("task.finished", goal.id, task.id, {"status": task.status, "error": task.error}))
                return task

        return list(await asyncio.gather(*(execute(task) for task in tasks)))

    async def _evaluate(self, goal: AgentGoal, tasks: list[AgentTask]) -> dict[str, Any]:
        if self.evaluator:
            return await self.evaluator(goal, tasks)
        failed = [task for task in tasks if task.status == "failed"]
        if failed:
            return {"verified": False, "reason": failed[0].error or "task failed"}
        # The underlying AIRVIS workflow is authoritative for completion.
        successful = [task.result for task in tasks if task.status == "completed"]
        return {"verified": bool(successful), "done": bool(successful), "result": successful[-1] if successful else None}

    async def _repair(self, goal: AgentGoal, reason: str) -> None:
        if self._repairs >= self.policy.max_repairs:
            goal.status = "exhausted"
            return
        self._repairs += 1
        await self._emit(KernelEvent("goal.repair", goal.id, data={"reason": reason, "repair": self._repairs}))
        goal.history.append({"repair": self._repairs, "reason": reason})

    async def _delegate(self, goal: AgentGoal, prompts: list[str], *, strategy: str | None) -> None:
        remaining = self.policy.max_delegations - self._delegations
        for prompt in prompts[: max(0, remaining)]:
            self._delegations += 1
            task = AgentTask(goal_id=goal.id, prompt=str(prompt), agent="sub-agent")
            self.tasks[task.id] = task
        await self._emit(KernelEvent("agent.delegated", goal.id, data={"count": len(prompts[: max(0, remaining)])}))


__all__ = ["AgentKernel", "AgentGoal", "AgentTask", "KernelEvent", "KernelPolicy"]
