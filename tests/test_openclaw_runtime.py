from pathlib import Path

from airvis.openclaw import OpenClaw, OpenClawOptions


def test_openclaw_is_not_an_external_cli_backend(tmp_path: Path):
    claw = OpenClaw(
        tmp_path,
        options=OpenClawOptions(use_llm_planner=False),
    )

    assert claw.name == "AIRVIS OpenClaw"
    assert claw.engine.backends.has("native")
    assert not claw.engine.backends.has("openclaw")
    assert "dag_orchestration" in claw.describe()["capabilities"]
    assert "review" in claw.describe()["capabilities"]
    assert "repair" in claw.describe()["capabilities"]


def test_openclaw_exposes_real_agent_and_tool_registries(tmp_path: Path):
    claw = OpenClaw(tmp_path, options=OpenClawOptions(use_llm_planner=False))
    description = claw.describe()

    assert "coder" in description["agents"]
    assert "reviewer" in description["agents"]
    assert description["tools"] > 0
