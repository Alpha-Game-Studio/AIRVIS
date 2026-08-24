import asyncio

from airvis.agent_kernel import AgentKernel, AgentTask


def test_kernel_executes_and_verifies_goal():
    calls = []

    async def executor(request, **kwargs):
        calls.append((request, kwargs))
        return {"ok": True, "output": request}

    async def planner(goal):
        return [AgentTask(goal_id=goal.id, prompt="implement feature", agent="coder")]

    kernel = AgentKernel(executor, planner=planner)
    goal = asyncio.run(kernel.run("build feature", max_iterations=2))

    assert goal.status == "completed"
    assert calls
    assert calls[0][1]["agent"] == "coder"
