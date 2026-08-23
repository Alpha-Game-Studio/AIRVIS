"""Test-execution tool used by the tester agent and the review quality gate."""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
from typing import Any

from .base import RiskLevel, Tool, ToolContext, ToolResult

MAX_OUTPUT_CHARS = 60_000
_PYTEST_SUMMARY = re.compile(
    r"(?:(?P<passed>\d+) passed)?[^\n]*?(?:(?P<failed>\d+) failed)?[^\n]*?(?:(?P<errors>\d+) error)?"
)


class TestRunTool(Tool):
    name = "test.run"
    description = "Run the project's Python test suite and return a structured result."
    risk = RiskLevel.MEDIUM
    required_permissions = frozenset({"test.run"})
    tags = frozenset({"test", "execute"})
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Test file or directory"},
            "framework": {"type": "string", "description": "auto | pytest | unittest"},
            "timeout": {"type": "number"},
            "extra_args": {"type": "array"},
        },
        "required": [],
    }

    async def run(
        self,
        context: ToolContext,
        path: str = "",
        framework: str = "auto",
        timeout: float | None = None,
        extra_args: list[str] | None = None,
    ) -> ToolResult:
        target = str(context.resolve_path(path)) if path else ""
        chosen = _select_framework(framework)
        if chosen == "pytest":
            argv = [sys.executable, "-m", "pytest", "-q", "--no-header"]
            if target:
                argv.append(target)
        else:
            argv = [sys.executable, "-m", "unittest", "discover", "-v"]
            if target:
                argv.extend(["-s", target])
        argv.extend(extra_args or [])

        limit = float(timeout or max(context.timeout, 300.0))
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(context.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=limit)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise

        text = stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
        code = process.returncode or 0
        counts = _parse_counts(text, chosen)
        passed = code == 0
        return ToolResult(
            tool=self.name,
            ok=passed,
            output={"framework": chosen, "exit_code": code, "passed": passed, **counts, "output": text[-8000:]},
            error=None if passed else f"test suite failed (exit code {code})",
            metadata={"framework": chosen, "exit_code": code, **counts},
            artifacts=[{"type": "test_result", "name": f"{chosen}-run", "content": text[-8000:]}],
        )


def _select_framework(requested: str) -> str:
    token = (requested or "auto").strip().lower()
    if token in {"pytest", "unittest"}:
        return token
    if shutil.which("pytest") or _module_available("pytest"):
        return "pytest"
    return "unittest"


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def _parse_counts(text: str, framework: str) -> dict[str, Any]:
    if framework == "pytest":
        return {
            "passed_count": _first_int(r"(\d+) passed", text),
            "failed_count": _first_int(r"(\d+) failed", text),
            "error_count": _first_int(r"(\d+) error", text),
            "skipped_count": _first_int(r"(\d+) skipped", text),
        }
    ran = _first_int(r"Ran (\d+) test", text)
    failures = _first_int(r"failures=(\d+)", text)
    errors = _first_int(r"errors=(\d+)", text)
    return {
        "passed_count": max(0, ran - failures - errors),
        "failed_count": failures,
        "error_count": errors,
        "skipped_count": _first_int(r"skipped=(\d+)", text),
    }


def _first_int(pattern: str, text: str) -> int:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else 0


def testing_tools() -> list[Tool]:
    return [TestRunTool()]


__all__ = ["TestRunTool", "testing_tools"]
