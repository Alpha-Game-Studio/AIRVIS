"""The default agent roster.

These are *registry entries*, not pipeline hardcoding: the planner only asks for
capabilities, and :class:`~airvis.agents.router.AgentRouter` picks whichever
registered agent scores best. Replacing or extending the roster changes routing
without touching the orchestrator.
"""

from __future__ import annotations

from typing import Any

from ..core.config import AgentsConfig
from .spec import AgentSpec

#: role -> declaration. ``prefers`` steers which registered provider is bound.
DEFAULT_ROSTER: list[dict[str, Any]] = [
    {
        "id": "researcher",
        "role": "research",
        "description": "저장소와 외부 자료를 조사해 사실을 수집합니다.",
        "capabilities": {"research", "search", "analysis", "read"},
        "tools": {
            "filesystem.search", "filesystem.read", "filesystem.grep",
            "code.analyze", "web.fetch", "github.search", "system.info",
        },
        "permissions": {"network"},
        "priority": 1.0,
        "quality": 0.5,
        "prefers": "cheap",
        "system_prompt": (
            "당신은 AIRVIS 리서처입니다. 도구로 수집한 사실만 근거로 삼고, "
            "추측은 '추정'이라고 명시하세요."
        ),
    },
    {
        "id": "debugger",
        "role": "debug",
        "description": "결함을 재현하고 원인을 특정합니다.",
        "capabilities": {"debug", "analysis", "diagnosis", "read"},
        "tools": {"filesystem.read", "filesystem.grep", "filesystem.search", "code.analyze",
                  "terminal.execute", "test.run", "git.diff"},
        "permissions": {"terminal.execute", "test.run"},
        "priority": 1.1,
        "quality": 0.65,
        "prefers": "quality",
        "system_prompt": "당신은 AIRVIS 디버거입니다. 증상이 아니라 근본 원인을 파일:라인 단위로 지목하세요.",
    },
    {
        "id": "architect",
        "role": "design",
        "description": "수정 방향과 설계를 결정합니다.",
        "capabilities": {"design", "architecture", "planning", "analysis"},
        "tools": {"filesystem.read", "filesystem.search", "filesystem.grep", "code.analyze"},
        "permissions": set(),
        "priority": 1.1,
        "quality": 0.7,
        "prefers": "quality",
        "system_prompt": "당신은 AIRVIS 아키텍트입니다. 최소 변경으로 문제를 해결하는 설계를 제시하세요.",
    },
    {
        "id": "coder",
        "role": "code",
        "description": "코드를 실제로 수정합니다.",
        "capabilities": {"code", "edit", "implementation", "fix"},
        "tools": {"filesystem.read", "filesystem.write", "filesystem.search", "filesystem.grep",
                  "code.analyze", "git.diff", "git.status"},
        "permissions": {"filesystem.write"},
        "priority": 1.2,
        "quality": 0.75,
        "prefers": "quality",
        "system_prompt": "당신은 AIRVIS 코더입니다. 요청 범위 밖의 파일은 건드리지 마세요.",
    },
    {
        "id": "tester",
        "role": "test",
        "description": "테스트를 실행하고 결과를 판정합니다.",
        "capabilities": {"test", "verification", "execution"},
        "tools": {"test.run", "terminal.execute", "filesystem.read", "git.status"},
        "permissions": {"test.run", "terminal.execute"},
        "priority": 1.0,
        "quality": 0.5,
        "prefers": "cheap",
        "system_prompt": "당신은 AIRVIS 테스터입니다. 테스트 출력만 근거로 통과/실패를 보고하세요.",
    },
    {
        "id": "reviewer",
        "role": "review",
        "description": "산출물을 품질 게이트 기준으로 심사합니다.",
        "capabilities": {"review", "quality", "security", "analysis"},
        "tools": {"filesystem.read", "filesystem.grep", "git.diff", "code.analyze"},
        "permissions": set(),
        "priority": 1.1,
        "quality": 0.85,
        "prefers": "premium",
        "system_prompt": "당신은 AIRVIS 리뷰어입니다. 통과시킬 이유가 아니라 반려할 이유를 먼저 찾으세요.",
    },
    {
        "id": "committer",
        "role": "vcs",
        "description": "변경 사항을 스테이징하고 커밋합니다.",
        "capabilities": {"git", "commit", "vcs"},
        "tools": {"git.status", "git.diff", "git.commit", "git.push", "filesystem.read"},
        "permissions": {"git.write", "git.push", "network"},
        "priority": 0.9,
        "quality": 0.5,
        "prefers": "cheap",
        "system_prompt": "당신은 AIRVIS 커미터입니다. 커밋 메시지는 변경 내용을 그대로 기술하세요.",
    },
    {
        "id": "reporter",
        "role": "report",
        "description": "워크플로 결과를 사용자용 보고서로 정리합니다.",
        "capabilities": {"report", "summary", "writing", "chat"},
        "tools": {"filesystem.read", "filesystem.write"},
        "permissions": {"filesystem.write"},
        "priority": 1.0,
        "quality": 0.55,
        "prefers": "balanced",
        "system_prompt": "당신은 AIRVIS 리포터입니다. 실제 실행 결과만 요약하고 없는 성과를 만들지 마세요.",
    },
    {
        "id": "generalist",
        "role": "general",
        "description": "전용 에이전트가 없을 때 처리하는 범용 에이전트입니다.",
        "capabilities": {
            "chat", "general", "summary", "research", "analysis", "read", "report",
            "writing", "code", "edit", "test", "review", "debug", "design", "planning",
            "search", "quality", "verification", "execution", "diagnosis",
            "implementation", "fix", "architecture", "security",
        },
        "tools": {"filesystem.read", "filesystem.search", "filesystem.grep", "system.info", "code.analyze"},
        "permissions": set(),
        "priority": 0.4,
        "quality": 0.3,
        "prefers": "cheap",
        "system_prompt": "당신은 AIRVIS 범용 에이전트입니다. 전문 에이전트가 없을 때만 호출됩니다.",
    },
]


def choose_provider(providers: Any, preference: str) -> str | None:
    """Bind a role preference to a concrete registered provider id."""
    if providers is None or len(providers) == 0:
        return None
    available = list(providers)
    if preference in {"quality", "premium"}:
        ranked = sorted(available, key=lambda item: (-item.quality, item.id))
    elif preference == "cheap":
        ranked = sorted(
            available,
            key=lambda item: (
                not item.local,
                (item.cost_per_million_input + item.cost_per_million_output),
                item.id,
            ),
        )
    else:  # balanced
        ranked = sorted(
            available,
            key=lambda item: (
                -(item.quality - (item.cost_per_million_input + item.cost_per_million_output) / 40.0),
                item.id,
            ),
        )
    return ranked[0].id if ranked else None


def default_agents(
    *,
    providers: Any = None,
    backends: Any = None,
    tools: Any = None,
    config: AgentsConfig | None = None,
) -> list[AgentSpec]:
    """Build the roster, binding each agent to a registered backend and provider."""
    settings = config or AgentsConfig()
    backend_id = settings.default_backend
    if backends is not None and not backends.has(backend_id):
        available = backends.names()
        backend_id = available[0] if available else backend_id

    agents: list[AgentSpec] = []
    for entry in DEFAULT_ROSTER:
        declared_tools = set(entry["tools"])
        if tools is not None:
            declared_tools = {name for name in declared_tools if tools.has(name)}
        provider_id = choose_provider(providers, str(entry.get("prefers", "balanced")))
        provider = providers.get(provider_id) if (providers is not None and provider_id) else None
        agents.append(
            AgentSpec(
                id=entry["id"],
                role=entry["role"],
                description=entry["description"],
                capabilities=frozenset(entry["capabilities"]),
                tools=frozenset(declared_tools),
                permissions=frozenset(entry["permissions"]),
                backend_id=backend_id,
                provider_id=provider_id,
                model=provider.default_model if provider is not None else None,
                system_prompt=entry["system_prompt"],
                priority=float(entry["priority"]),
                quality=float(entry["quality"]),
                max_concurrency=settings.default_max_concurrency,
                timeout=settings.default_timeout,
                tags=frozenset({entry["role"]}),
            )
        )
    return agents


__all__ = ["DEFAULT_ROSTER", "choose_provider", "default_agents"]
