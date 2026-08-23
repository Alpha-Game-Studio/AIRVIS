"""Task decomposition.

The planner turns a request into a task graph expressed purely in
*capabilities* and *tool steps*. It never names an agent — that is the router's
job — so adding or removing agents changes execution without touching plans.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..core.config import AgentsConfig, WorkflowConfig
from ..core.errors import PlanningError
from ..providers.base import GenerationRequest, Message
from .task import Plan, RetryPolicy, Task, ToolStep


@dataclass(frozen=True)
class Intent:
    """One recognisable unit of work inside a request."""

    key: str
    capabilities: tuple[str, ...]
    description: str
    patterns: tuple[str, ...]
    tools: tuple[str, ...] = ()
    stage: int = 0
    priority: float = 1.0
    #: finalizers still run when an upstream task failed
    finalizer: bool = False

    def matches(self, text: str) -> bool:
        return any(re.search(pattern, text) for pattern in self.patterns)


#: Ordered catalogue; ``stage`` defines the dependency layering.
INTENTS: tuple[Intent, ...] = (
    Intent(
        key="inspect",
        capabilities=("research", "read"),
        description="저장소 구조와 관련 파일을 조사한다",
        patterns=(r"분석", r"조사", r"탐색", r"살펴", r"파악", r"검토", r"analy[sz]e", r"inspect", r"explore", r"review the (repo|code)"),
        tools=("filesystem.search", "system.info"),
        stage=0,
        priority=1.0,
    ),
    Intent(
        key="static_analysis",
        capabilities=("analysis", "read"),
        description="정적 분석으로 결함 후보를 수집한다",
        patterns=(r"버그", r"결함", r"오류", r"에러", r"문제", r"bug", r"defect", r"issue", r"error", r"분석", r"analy[sz]e"),
        tools=("code.analyze",),
        stage=0,
        priority=1.2,
    ),
    Intent(
        key="diagnose",
        capabilities=("debug", "diagnosis"),
        description="수집된 후보에서 실제 원인을 특정한다",
        patterns=(r"버그", r"원인", r"디버그", r"고[치쳐]", r"수정", r"fix", r"bug", r"debug", r"root cause"),
        stage=1,
        priority=1.2,
    ),
    Intent(
        key="design",
        capabilities=("design", "planning"),
        description="수정 방안을 설계한다",
        patterns=(r"설계", r"방안", r"제안", r"계획", r"design", r"propose", r"plan"),
        stage=2,
        priority=1.0,
    ),
    Intent(
        key="implement",
        capabilities=("code", "edit"),
        description="설계에 따라 코드를 수정한다",
        # Deliberately narrow: generic verbs such as "작성"/"write" also appear in
        # report requests and must not spawn a code-editing task.
        patterns=(r"수정", r"고[치쳐]", r"구현", r"패치", r"리팩터", r"fix", r"implement", r"patch", r"refactor"),
        stage=3,
        priority=1.3,
    ),
    Intent(
        key="test",
        capabilities=("test", "verification"),
        description="테스트를 실행해 결과를 확인한다",
        patterns=(r"테스트", r"검증", r"실행해", r"test", r"verify", r"run the tests"),
        tools=("test.run",),
        stage=4,
        priority=1.2,
    ),
    Intent(
        key="review",
        capabilities=("review", "quality"),
        description="변경 결과를 심사한다",
        patterns=(r"리뷰", r"심사", r"품질", r"review", r"audit"),
        stage=5,
        priority=1.0,
    ),
    Intent(
        key="commit",
        capabilities=("git", "commit"),
        description="변경 사항을 커밋한다",
        patterns=(r"커밋", r"commit", r"체크인"),
        tools=("git.status", "git.diff"),
        stage=6,
        priority=0.9,
    ),
    Intent(
        key="report",
        capabilities=("report", "summary"),
        description="최종 보고서를 작성한다",
        patterns=(r"보고", r"요약", r"정리", r"리포트", r"report", r"summar"),
        stage=7,
        priority=1.0,
        finalizer=True,
    ),
)

#: Requests that match nothing above still need one executable task.
FALLBACK_INTENT = Intent(
    key="respond",
    capabilities=("chat",),
    description="요청에 직접 답한다",
    patterns=(),
    stage=0,
)


class Planner:
    """Deterministic, offline-capable task decomposition."""

    strategy = "heuristic"

    def __init__(
        self,
        *,
        agents_config: AgentsConfig | None = None,
        workflow_config: WorkflowConfig | None = None,
        max_tasks: int = 12,
    ) -> None:
        self.agents_config = agents_config or AgentsConfig()
        self.workflow_config = workflow_config or WorkflowConfig()
        self.max_tasks = max_tasks

    # -- public API ------------------------------------------------------------

    async def plan(self, request: str, *, workflow_id: str | None = None, **_: Any) -> Plan:
        text = (request or "").strip()
        if not text:
            raise PlanningError("cannot plan an empty request")
        intents = self._detect(text)
        tasks = self._build_tasks(text, intents, workflow_id)
        return Plan(request=text, tasks=tasks, strategy=self.strategy)

    #: a replanned task may itself be replanned at most this many levels deep
    max_replan_depth = 1

    async def replan(self, task: Task, reason: str, *, workflow_id: str | None = None) -> list[Task]:
        """Split a failed task into smaller steps so a retry can make progress.

        Bounded by :attr:`max_replan_depth`: without it, every generated subtask
        could replan again and the graph would grow without end.
        """
        depth = int(task.metadata.get("replan_depth", 0))
        if depth >= self.max_replan_depth:
            return []
        base = task.metadata.get("replan_root_name") or task.name
        pieces = [
            ("사실 수집", f"'{base}' 작업에 필요한 파일과 사실을 먼저 수집한다"),
            ("작업 수행", f"수집한 사실을 근거로 '{base}' 작업을 수행한다 (직전 실패 사유: {reason})"),
        ]
        created: list[Task] = []
        previous: str | None = None
        for index, (label, description) in enumerate(pieces):
            subtask = Task(
                description=description,
                name=f"{base}:{label}",
                workflow_id=workflow_id or task.workflow_id,
                required_capabilities=list(task.required_capabilities) if index else ["research", "read"],
                required_tools=list(task.required_tools) if index else [],
                tool_plan=list(task.tool_plan) if index else [ToolStep("filesystem.search", {"pattern": "**/*.py"})],
                dependencies=[previous] if previous else list(task.dependencies),
                priority=task.priority,
                timeout=task.timeout,
                retry_policy=RetryPolicy(max_attempts=max(1, task.retry_policy.max_attempts - 1)),
                metadata={
                    "replan_of": task.id,
                    "replan_depth": depth + 1,
                    "replan_root_name": base,
                    "reason": reason,
                },
            )
            created.append(subtask)
            previous = subtask.id
        return created

    # -- internals -------------------------------------------------------------

    def _detect(self, text: str) -> list[Intent]:
        lowered = text.lower()
        matched = [intent for intent in INTENTS if intent.matches(lowered)]
        if not matched:
            return [FALLBACK_INTENT]
        # A pure question ("무엇인가요?") should not spawn a build pipeline.
        if len(matched) == 1 and matched[0].key in {"inspect", "static_analysis"} and _is_question(lowered):
            return [FALLBACK_INTENT]
        if not any(intent.key == "report" for intent in matched) and len(matched) > 1:
            matched.append(next(intent for intent in INTENTS if intent.key == "report"))
        return matched[: self.max_tasks]

    def _build_tasks(self, request: str, intents: list[Intent], workflow_id: str | None) -> list[Task]:
        by_stage: dict[int, list[Task]] = {}
        tasks: list[Task] = []
        for intent in sorted(intents, key=lambda item: item.stage):
            task = Task(
                description=f"{intent.description} (요청: {_shorten(request, 160)})",
                name=intent.key,
                workflow_id=workflow_id,
                required_capabilities=list(intent.capabilities),
                required_tools=list(intent.tools),
                tool_plan=[ToolStep(tool, _default_arguments(tool), optional=True) for tool in intent.tools],
                priority=intent.priority,
                finalizer=intent.finalizer,
                timeout=self.agents_config.default_timeout,
                retry_policy=RetryPolicy(max_attempts=2 if intent.stage == 0 else 3),
                metadata={"intent": intent.key, "stage": intent.stage},
            )
            previous_stages = [stage for stage in by_stage if stage < intent.stage]
            if previous_stages:
                nearest = max(previous_stages)
                task.dependencies = [item.id for item in by_stage[nearest]]
            by_stage.setdefault(intent.stage, []).append(task)
            tasks.append(task)
        return tasks


class LLMPlanner(Planner):
    """Asks a provider for a plan and falls back to the heuristic planner.

    The model output is validated against the tool registry and capability
    vocabulary; anything malformed falls back rather than producing a plan that
    references things that do not exist.
    """

    strategy = "llm"

    def __init__(self, providers: Any, *, tools: Any = None, provider_id: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.providers = providers
        self.tools = tools
        self.provider_id = provider_id

    async def plan(self, request: str, *, workflow_id: str | None = None, **kwargs: Any) -> Plan:
        text = (request or "").strip()
        if not text:
            raise PlanningError("cannot plan an empty request")
        try:
            generation = await self.providers.generate(
                GenerationRequest(
                    messages=[Message("system", _PLANNER_SYSTEM), Message("user", text)],
                    response_format="json",
                    temperature=0.0,
                ),
                provider_id=self.provider_id,
                workflow_id=workflow_id,
            )
            tasks = self._parse(generation.text, workflow_id)
        except Exception:
            tasks = []
        if not tasks:
            fallback = await super().plan(text, workflow_id=workflow_id)
            fallback.metadata["llm_planner"] = "fell back to heuristic decomposition"
            return fallback
        return Plan(request=text, tasks=tasks, strategy=self.strategy)

    def _parse(self, raw: str, workflow_id: str | None) -> list[Task]:
        payload = _extract_json(raw)
        entries = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            return []
        tasks: list[Task] = []
        index_to_id: dict[str, str] = {}
        for position, entry in enumerate(entries[: self.max_tasks]):
            if not isinstance(entry, dict) or not entry.get("description"):
                continue
            task = Task(
                description=str(entry["description"]),
                name=str(entry.get("name") or f"task-{position + 1}"),
                workflow_id=workflow_id,
                required_capabilities=[str(item) for item in entry.get("capabilities") or []],
                required_tools=[
                    str(item) for item in entry.get("tools") or [] if self.tools is None or self.tools.has(str(item))
                ],
                priority=float(entry.get("priority", 1.0)),
                timeout=self.agents_config.default_timeout,
                metadata={"planner": "llm"},
            )
            index_to_id[str(entry.get("id", position + 1))] = task.id
            tasks.append(task)
        # ``entries`` may be longer than ``tasks``: malformed entries were dropped.
        for entry, task in zip(entries, tasks, strict=False):
            references = entry.get("dependencies") or entry.get("depends_on") or []
            task.dependencies = [index_to_id[str(item)] for item in references if str(item) in index_to_id]
        return tasks


_PLANNER_SYSTEM = """You decompose a user request into an executable task graph.
Reply with JSON only: {"tasks": [{"id": 1, "name": "...", "description": "...",
"capabilities": ["research"], "tools": ["filesystem.read"], "dependencies": [],
"priority": 1.0}]}.
Capabilities must come from: research, read, search, analysis, debug, diagnosis,
design, planning, code, edit, implementation, fix, test, verification, review,
quality, security, git, commit, report, summary, chat.
Keep the plan under 10 tasks. Use dependencies to express ordering."""


def _default_arguments(tool: str) -> dict[str, Any]:
    if tool == "filesystem.search":
        return {"pattern": "**/*.py", "limit": 200}
    if tool == "code.analyze":
        return {"pattern": "**/*.py", "min_severity": "medium"}
    if tool == "git.diff":
        return {}
    return {}


def _is_question(text: str) -> bool:
    return text.rstrip().endswith(("?", "？")) or bool(
        re.search(r"(무엇|뭐야|뭔가요|알려줘|설명해|what is|how does|why)", text)
    )


def _extract_json(raw: str) -> Any:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return {}


def _shorten(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


__all__ = ["FALLBACK_INTENT", "INTENTS", "Intent", "LLMPlanner", "Planner"]
