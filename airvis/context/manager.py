"""Unified context assembly.

The manager decides *what a task gets to see*: system rules, the original
request, workspace facts, upstream results, artifact references and review
notes — compressed to fit a budget instead of pasting whole histories.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..artifacts.manager import ArtifactManager
from ..core.config import ContextConfig
from ..providers.base import Message

TRUNCATION_MARK = "…[truncated]"


@dataclass
class ContextBundle:
    """Everything a single task execution is allowed to read."""

    system: str = ""
    user_request: str = ""
    workspace: str = ""
    task: str = ""
    previous_results: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    agent_messages: list[dict[str, str]] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_messages(self) -> list[Message]:
        messages: list[Message] = []
        if self.system:
            messages.append(Message("system", self.system))
        sections: list[str] = []
        if self.workspace:
            sections.append(f"[작업 공간]\n{self.workspace}")
        if self.memories:
            sections.append("[장기 기억]\n" + "\n".join(f"- {item}" for item in self.memories))
        if self.previous_results:
            rendered = "\n".join(
                f"- {item.get('name') or item.get('task_id')}: {_shorten(str(item.get('output', '')), 800)}"
                for item in self.previous_results
            )
            sections.append(f"[선행 작업 결과]\n{rendered}")
        if self.artifacts:
            rendered = "\n".join(
                f"- {item.get('id')} ({item.get('type')}) {item.get('name')}" for item in self.artifacts
            )
            sections.append(f"[아티팩트 참조]\n{rendered}")
        if self.review_notes:
            sections.append("[이전 리뷰 지적사항]\n" + "\n".join(f"- {note}" for note in self.review_notes))
        if self.user_request:
            sections.append(f"[사용자 원 요청]\n{self.user_request}")
        if sections:
            messages.append(Message("system", "\n\n".join(sections)))
        for message in self.agent_messages:
            messages.append(Message(str(message.get("role", "user")), str(message.get("content", ""))))
        for result in self.tool_results:
            messages.append(Message("tool", json.dumps(result, ensure_ascii=False, default=str)))
        messages.append(Message("user", self.task or self.user_request))
        return messages

    def size(self) -> int:
        return sum(len(message.content) for message in self.to_messages())

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "user_request": self.user_request,
            "workspace": self.workspace,
            "task": self.task,
            "previous_results": self.previous_results,
            "artifacts": self.artifacts,
            "tool_results": self.tool_results,
            "agent_messages": self.agent_messages,
            "review_notes": self.review_notes,
            "memories": self.memories,
            "metadata": self.metadata,
        }


class ContextManager:
    """Builds and compresses :class:`ContextBundle` objects."""

    def __init__(
        self,
        config: ContextConfig | None = None,
        *,
        artifacts: ArtifactManager | None = None,
        workspace: Path | str | None = None,
        memory: Any = None,
    ) -> None:
        self.config = config or ContextConfig()
        self.artifacts = artifacts
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.memory = memory
        #: workflow_id -> rolling conversation shared by every task in the run
        self._workflow_messages: dict[str, list[dict[str, str]]] = {}

    # -- workflow scope --------------------------------------------------------

    def start_workflow(self, workflow_id: str, request: str) -> None:
        self._workflow_messages[workflow_id] = [{"role": "user", "content": request}]

    def record_message(self, workflow_id: str, role: str, content: str) -> None:
        history = self._workflow_messages.setdefault(workflow_id, [])
        history.append({"role": role, "content": content})
        limit = max(2, self.config.max_messages)
        if len(history) > limit:
            del history[: len(history) - limit]

    def workflow_messages(self, workflow_id: str) -> list[dict[str, str]]:
        return list(self._workflow_messages.get(workflow_id, []))

    def end_workflow(self, workflow_id: str) -> None:
        self._workflow_messages.pop(workflow_id, None)

    # -- assembly --------------------------------------------------------------

    def build(
        self,
        task: Any,
        *,
        request: str = "",
        workflow_id: str | None = None,
        agent: Any = None,
        upstream_results: list[Any] | None = None,
        review_notes: list[str] | None = None,
        include_history: bool = False,
    ) -> ContextBundle:
        bundle = ContextBundle(
            system=_system_prompt(agent),
            user_request=request,
            workspace=self._workspace_summary(),
            task=getattr(task, "description", str(task)),
            review_notes=list(review_notes or []),
        )

        for result in (upstream_results or [])[-self.config.max_previous_results :]:
            bundle.previous_results.append(
                {
                    "task_id": getattr(result, "task_id", None),
                    "name": (getattr(result, "metadata", {}) or {}).get("task_name"),
                    "output": getattr(result, "output", ""),
                    "ok": getattr(result, "ok", True),
                }
            )

        if self.config.include_artifacts and self.artifacts is not None and workflow_id:
            bundle.artifacts = [artifact.reference() for artifact in self.artifacts.list(workflow_id=workflow_id)][-20:]

        if self.memory is not None:
            try:
                bundle.memories = [str(item.get("content", "")) for item in self.memory.list()[:5]]
            except Exception:  # memory is best-effort context, never fatal
                bundle.memories = []

        if include_history and workflow_id:
            bundle.agent_messages = self.workflow_messages(workflow_id)

        return self.compress(bundle)

    # -- compression -----------------------------------------------------------

    def compress(self, bundle: ContextBundle) -> ContextBundle:
        mode = (self.config.compression or "truncate").lower()
        if mode == "none":
            return bundle
        budget = max(1000, self.config.max_chars)
        if bundle.size() <= budget:
            return bundle

        # Drop the cheapest signal first, then progressively shorten payloads.
        bundle.tool_results = bundle.tool_results[-3:]
        if bundle.size() > budget:
            bundle.agent_messages = bundle.agent_messages[-4:]
        if bundle.size() > budget:
            bundle.artifacts = bundle.artifacts[-8:]
        if bundle.size() > budget:
            per_result = max(200, budget // max(1, len(bundle.previous_results) * 3))
            for item in bundle.previous_results:
                item["output"] = _shorten(str(item.get("output", "")), per_result)
        if bundle.size() > budget:
            bundle.previous_results = bundle.previous_results[-2:]
        if bundle.size() > budget:
            bundle.memories = []
            bundle.workspace = _shorten(bundle.workspace, 400)
        if bundle.size() > budget:
            bundle.user_request = _shorten(bundle.user_request, 1000)
        bundle.metadata["compressed"] = True
        bundle.metadata["compression"] = mode
        return bundle

    def _workspace_summary(self) -> str:
        lines = [f"경로: {self.workspace}"]
        try:
            entries = sorted(
                item.name for item in self.workspace.iterdir() if not item.name.startswith(".")
            )[:40]
        except OSError:  # pragma: no cover - unreadable workspace
            entries = []
        if entries:
            lines.append("최상위 항목: " + ", ".join(entries))
        return "\n".join(lines)


def _system_prompt(agent: Any) -> str:
    if agent is None:
        return "당신은 AIRVIS 오케스트레이션 엔진의 실행 에이전트입니다. 주어진 작업만 정확히 수행하세요."
    custom = getattr(agent, "system_prompt", "")
    if custom:
        return custom
    role = getattr(agent, "role", "agent")
    return (
        f"당신은 AIRVIS의 '{role}' 역할 에이전트입니다. "
        "주어진 작업 범위를 벗어나지 말고, 도구 관찰 결과에 근거해서만 답하세요."
    )


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + TRUNCATION_MARK


__all__ = ["ContextBundle", "ContextManager"]
