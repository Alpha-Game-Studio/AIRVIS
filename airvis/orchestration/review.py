"""Review as a real quality gate.

Every dimension is evaluated against evidence that actually exists (tool
results, artifacts, test output) so a reviewer can — and does — reject work.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.config import ReviewConfig
from ..core.events import EventBus, EventType
from ..providers.base import GenerationRequest, Message
from .task import Task, TaskResult

#: dimension -> weight in the final score
DIMENSION_WEIGHTS: dict[str, float] = {
    "correctness": 3.0,
    "completeness": 2.0,
    "security": 2.5,
    "tests": 2.0,
    "requirements": 1.5,
    "regressions": 1.5,
    "code_quality": 1.0,
}

_ERROR_MARKERS = ("Traceback (most recent call last)", "SyntaxError", "ModuleNotFoundError", "NameError:")
_SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{12,}")


@dataclass
class ReviewIssue:
    dimension: str
    severity: str  # info | minor | major | critical
    message: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "severity": self.severity,
            "message": self.message,
            "evidence": self.evidence[:500],
        }


@dataclass
class ReviewResult:
    status: str = "PASS"  # PASS | FAIL
    score: float = 1.0
    issues: list[ReviewIssue] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    dimensions: dict[str, float] = field(default_factory=dict)
    reviewer: str = "airvis.review"
    task_id: str | None = None
    reviewed_at: float = field(default_factory=time.time)

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def blocking_issues(self) -> list[ReviewIssue]:
        return [issue for issue in self.issues if issue.severity in {"major", "critical"}]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": round(self.score, 4),
            "passed": self.passed,
            "issues": [issue.to_dict() for issue in self.issues],
            "recommendations": self.recommendations,
            "dimensions": {key: round(value, 4) for key, value in self.dimensions.items()},
            "reviewer": self.reviewer,
            "task_id": self.task_id,
            "reviewed_at": self.reviewed_at,
        }


class ReviewSystem:
    """Scores a task result across the configured dimensions."""

    def __init__(
        self,
        config: ReviewConfig | None = None,
        *,
        providers: Any = None,
        artifacts: Any = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.config = config or ReviewConfig()
        self.providers = providers
        self.artifacts = artifacts
        self.event_bus = event_bus

    async def review_task(
        self,
        task: Task,
        result: TaskResult,
        *,
        workflow_id: str | None = None,
        request: str = "",
    ) -> ReviewResult:
        if not self.config.enabled:
            return ReviewResult(status="PASS", score=1.0, task_id=task.id, reviewer="disabled")

        self._emit(EventType.REVIEW_STARTED, task, workflow_id, status="started")
        issues: list[ReviewIssue] = []
        scores: dict[str, float] = {}

        for dimension in self.config.dimensions:
            evaluator = getattr(self, f"_check_{dimension}", None)
            if evaluator is None:
                continue
            score, found = evaluator(task, result, request)
            scores[dimension] = score
            issues.extend(found)

        if self.config.use_llm_reviewer and self.providers is not None:
            score, found = await self._llm_review(task, result, request)
            if score is not None:
                scores["llm"] = score
                issues.extend(found)

        total_weight = sum(DIMENSION_WEIGHTS.get(name, 1.0) for name in scores) or 1.0
        weighted = sum(DIMENSION_WEIGHTS.get(name, 1.0) * value for name, value in scores.items())
        final = weighted / total_weight

        # A dimension that substantially failed blocks even when the weighted
        # average stays high — otherwise one strong dimension could carry a
        # missing deliverable past the gate.
        blocking = [
            issue
            for issue in issues
            if issue.severity == "critical"
            or (issue.severity == "major" and scores.get(issue.dimension, 1.0) < 0.5)
        ]
        status = "FAIL" if blocking or final < self.config.min_score else "PASS"

        review = ReviewResult(
            status=status,
            score=final,
            issues=issues,
            recommendations=_recommendations(issues),
            dimensions=scores,
            task_id=task.id,
        )
        self._emit(
            EventType.REVIEW_COMPLETED, task, workflow_id, status=status,
            metadata={"score": round(final, 4), "issues": len(issues)},
        )
        return review

    async def review_workflow(
        self, request: str, tasks: list[Task], *, workflow_id: str | None = None
    ) -> ReviewResult:
        """Aggregate gate over the whole run."""
        issues: list[ReviewIssue] = []
        completed = [task for task in tasks if task.status.value == "completed"]
        failed = [task for task in tasks if task.status.value in {"failed", "cancelled"}]

        for task in failed:
            issues.append(
                ReviewIssue(
                    "completeness",
                    "critical" if task.status.value == "failed" else "major",
                    f"작업 '{task.name}'이(가) {task.status.value} 상태로 종료되었습니다",
                    (task.result.error if task.result else "") or "",
                )
            )
        for task in completed:
            if task.review and task.review.get("status") == "FAIL":
                issues.append(
                    ReviewIssue("requirements", "major", f"작업 '{task.name}'의 리뷰가 반려되었습니다")
                )

        ratio = len(completed) / len(tasks) if tasks else 0.0
        status = "PASS" if ratio >= self.config.min_score and not any(
            issue.severity == "critical" for issue in issues
        ) else "FAIL"
        return ReviewResult(
            status=status,
            score=ratio,
            issues=issues,
            recommendations=_recommendations(issues),
            dimensions={"completeness": ratio},
            reviewer="airvis.review.workflow",
            task_id=None,
        )

    # -- dimension evaluators ---------------------------------------------------

    def _check_correctness(self, task: Task, result: TaskResult, request: str) -> tuple[float, list[ReviewIssue]]:
        issues: list[ReviewIssue] = []
        if not result.ok:
            issues.append(ReviewIssue("correctness", "critical", "작업이 실패로 종료되었습니다", result.error or ""))
            return 0.0, issues
        failed_tools = [item for item in result.tool_results if not item.get("ok")]
        if failed_tools:
            names = ", ".join(str(item.get("tool")) for item in failed_tools)
            issues.append(ReviewIssue("correctness", "major", f"도구 실행 실패: {names}",
                                      str(failed_tools[0].get("error", ""))))
        marker = next((item for item in _ERROR_MARKERS if item in (result.output or "")), None)
        if marker:
            issues.append(ReviewIssue("correctness", "major", f"출력에 오류 흔적이 있습니다: {marker}"))
        score = 1.0 - 0.4 * len(issues)
        return max(0.0, score), issues

    def _check_completeness(self, task: Task, result: TaskResult, request: str) -> tuple[float, list[ReviewIssue]]:
        issues: list[ReviewIssue] = []
        if not (result.output or "").strip():
            issues.append(ReviewIssue("completeness", "major", "산출물이 비어 있습니다"))
            return 0.0, issues
        planned = {step.tool for step in task.tool_plan}
        executed = {str(item.get("tool")) for item in result.tool_results}
        missing = sorted(planned - executed)
        if missing:
            issues.append(
                ReviewIssue("completeness", "minor", f"계획된 도구가 실행되지 않았습니다: {', '.join(missing)}")
            )
            return 0.7, issues
        return 1.0, issues

    def _check_security(self, task: Task, result: TaskResult, request: str) -> tuple[float, list[ReviewIssue]]:
        issues: list[ReviewIssue] = []
        for item in result.tool_results:
            if item.get("error_code") in {"permission_denied", "approval_required"}:
                issues.append(
                    ReviewIssue("security", "minor", f"권한 게이트가 {item.get('tool')} 호출을 차단했습니다")
                )
            arguments = item.get("arguments") or {}
            content = str(arguments.get("content", ""))
            if content and _SECRET_PATTERN.search(content):
                issues.append(
                    ReviewIssue("security", "critical", f"{item.get('tool')} 호출이 자격증명으로 보이는 값을 기록합니다")
                )
        if _SECRET_PATTERN.search(result.output or ""):
            issues.append(ReviewIssue("security", "critical", "산출물에 자격증명으로 보이는 문자열이 포함되어 있습니다"))
        critical = any(issue.severity == "critical" for issue in issues)
        return (0.0 if critical else max(0.0, 1.0 - 0.15 * len(issues))), issues

    def _check_tests(self, task: Task, result: TaskResult, request: str) -> tuple[float, list[ReviewIssue]]:
        issues: list[ReviewIssue] = []
        runs = [item for item in result.tool_results if str(item.get("tool")) == "test.run"]
        if not runs:
            return 1.0, issues  # not a testing task; neutral
        failures = [item for item in runs if not item.get("ok")]
        if failures:
            detail = failures[0].get("error") or ""
            issues.append(ReviewIssue("tests", "critical", "테스트 스위트가 실패했습니다", str(detail)))
            return 0.0, issues
        return 1.0, issues

    def _check_requirements(self, task: Task, result: TaskResult, request: str) -> tuple[float, list[ReviewIssue]]:
        issues: list[ReviewIssue] = []
        if "code" in task.required_capabilities or "edit" in task.required_capabilities:
            wrote = any(str(item.get("tool")) == "filesystem.write" and item.get("ok") for item in result.tool_results)
            if not wrote and not result.artifact_ids:
                issues.append(
                    ReviewIssue("requirements", "major", "코드 수정 작업인데 파일 변경 산출물이 없습니다")
                )
                return 0.3, issues
        if "test" in task.required_capabilities:
            ran = any(str(item.get("tool")) == "test.run" for item in result.tool_results)
            if not ran:
                issues.append(ReviewIssue("requirements", "major", "테스트 작업인데 테스트를 실행하지 않았습니다"))
                return 0.3, issues
        return 1.0, issues

    def _check_regressions(self, task: Task, result: TaskResult, request: str) -> tuple[float, list[ReviewIssue]]:
        issues: list[ReviewIssue] = []
        for item in result.tool_results:
            if str(item.get("tool")) != "test.run":
                continue
            metadata = item.get("metadata") or {}
            failed = int(metadata.get("failed_count", 0)) + int(metadata.get("error_count", 0))
            if failed:
                issues.append(
                    ReviewIssue("regressions", "critical", f"테스트 {failed}건이 실패했습니다")
                )
                return 0.0, issues
        return 1.0, issues

    def _check_code_quality(self, task: Task, result: TaskResult, request: str) -> tuple[float, list[ReviewIssue]]:
        issues: list[ReviewIssue] = []
        for item in result.tool_results:
            if str(item.get("tool")) != "code.analyze" or not item.get("ok"):
                continue
            output = item.get("output") or {}
            findings = output.get("findings", []) if isinstance(output, dict) else []
            critical = [entry for entry in findings if entry.get("severity") == "critical"]
            high = [entry for entry in findings if entry.get("severity") == "high"]
            if critical:
                issues.append(
                    ReviewIssue("code_quality", "critical", f"치명적 정적 분석 결함 {len(critical)}건", str(critical[:2]))
                )
                return 0.0, issues
            if high:
                issues.append(ReviewIssue("code_quality", "minor", f"높은 심각도 결함 {len(high)}건", str(high[:2])))
                return 0.75, issues
        return 1.0, issues

    # -- optional LLM reviewer --------------------------------------------------

    async def _llm_review(
        self, task: Task, result: TaskResult, request: str
    ) -> tuple[float | None, list[ReviewIssue]]:
        prompt = (
            f"원 요청: {request}\n작업: {task.description}\n산출물:\n{(result.output or '')[:4000]}\n\n"
            "이 산출물이 작업을 충족하는지 0.0~1.0 점수와 문제점을 JSON으로 답하세요: "
            '{"score": 0.0, "issues": ["..."]}'
        )
        try:
            generation = await self.providers.generate(
                GenerationRequest(
                    messages=[Message("system", "You are a strict reviewer."), Message("user", prompt)],
                    response_format="json",
                    temperature=0.0,
                )
            )
        except Exception:
            return None, []
        import json

        try:
            payload = json.loads(generation.text[generation.text.find("{") : generation.text.rfind("}") + 1])
        except (ValueError, IndexError):
            return None, []
        score = float(payload.get("score", 1.0))
        issues = [
            ReviewIssue("llm", "major" if score < 0.5 else "minor", str(item))
            for item in (payload.get("issues") or [])[:5]
        ]
        return max(0.0, min(1.0, score)), issues

    def _emit(self, event_type: EventType, task: Task, workflow_id: str | None, **fields: Any) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(
            event_type, workflow_id=workflow_id, task_id=task.id, agent_id=task.assigned_agent_id, **fields
        )


def _recommendations(issues: list[ReviewIssue]) -> list[str]:
    advice: list[str] = []
    for issue in issues:
        if issue.severity not in {"major", "critical"}:
            continue
        if issue.dimension == "tests":
            advice.append("실패한 테스트를 먼저 통과시킨 뒤 다시 제출하세요.")
        elif issue.dimension == "security":
            advice.append("자격증명으로 보이는 값을 산출물에서 제거하세요.")
        elif issue.dimension == "requirements":
            advice.append("작업이 요구한 산출물(파일 변경, 테스트 실행)을 실제로 생성하세요.")
        elif issue.dimension == "correctness":
            advice.append("실패한 도구 호출의 원인을 해결한 뒤 재실행하세요.")
        elif issue.dimension == "code_quality":
            advice.append("정적 분석이 지적한 치명적 결함을 수정하세요.")
        else:
            advice.append(f"{issue.dimension}: {issue.message}")
    return list(dict.fromkeys(advice))


__all__ = ["DIMENSION_WEIGHTS", "ReviewIssue", "ReviewResult", "ReviewSystem"]
