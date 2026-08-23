"""Git tools with per-operation risk levels."""

from __future__ import annotations

import asyncio
import shutil

from ..core.errors import ToolExecutionError
from .base import RiskLevel, Tool, ToolContext, ToolResult

MAX_OUTPUT_CHARS = 60_000


async def _git(context: ToolContext, *args: str, timeout: float | None = None) -> tuple[int, str, str]:
    if shutil.which("git") is None:
        raise ToolExecutionError("git is not installed on this host", tool="git")
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(context.workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout or context.timeout or 60.0)
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS],
        stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS],
    )


def _result(tool: str, code: int, out: str, err: str, **metadata: object) -> ToolResult:
    return ToolResult(
        tool=tool,
        ok=code == 0,
        output=(out or err).strip(),
        error=None if code == 0 else (err.strip() or f"git exited with {code}"),
        metadata={"exit_code": code, **metadata},
    )


class GitStatusTool(Tool):
    name = "git.status"
    description = "Show the working tree status (porcelain)."
    risk = RiskLevel.SAFE
    tags = frozenset({"git", "read"})
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, context: ToolContext) -> ToolResult:
        code, out, err = await _git(context, "status", "--porcelain=v1", "--branch")
        return _result(self.name, code, out, err, dirty=bool(out.strip()))


class GitDiffTool(Tool):
    name = "git.diff"
    description = "Show a unified diff of the working tree or a given revision range."
    risk = RiskLevel.SAFE
    tags = frozenset({"git", "read"})
    parameters = {
        "type": "object",
        "properties": {
            "revision": {"type": "string", "description": "e.g. 'HEAD~1..HEAD'"},
            "staged": {"type": "boolean"},
            "path": {"type": "string"},
        },
        "required": [],
    }

    async def run(
        self, context: ToolContext, revision: str = "", staged: bool = False, path: str = ""
    ) -> ToolResult:
        args = ["diff", "--no-color"]
        if staged:
            args.append("--cached")
        if revision:
            args.append(revision)
        if path:
            args.extend(["--", path])
        code, out, err = await _git(context, *args)
        artifacts = (
            [{"type": "patch", "name": f"diff-{revision or 'worktree'}.patch", "content": out}] if out.strip() else []
        )
        result = _result(self.name, code, out, err, revision=revision, staged=staged)
        result.artifacts = artifacts
        return result


class GitLogTool(Tool):
    name = "git.log"
    description = "Show recent commits in a compact format."
    risk = RiskLevel.SAFE
    tags = frozenset({"git", "read"})
    parameters = {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}

    async def run(self, context: ToolContext, limit: int = 20) -> ToolResult:
        capped = max(1, min(int(limit), 200))
        code, out, err = await _git(context, "log", f"-{capped}", "--pretty=format:%h %an %ad %s", "--date=short")
        return _result(self.name, code, out, err, limit=capped)


class GitCloneTool(Tool):
    name = "git.clone"
    description = "Clone a remote repository into the workspace."
    risk = RiskLevel.MEDIUM
    required_permissions = frozenset({"git.write", "network"})
    network = True
    tags = frozenset({"git", "network"})
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "destination": {"type": "string"}, "depth": {"type": "integer"}},
        "required": ["url"],
    }

    async def run(self, context: ToolContext, url: str, destination: str = "", depth: int = 0) -> ToolResult:
        if not str(url).startswith(("https://", "http://", "git@", "ssh://")):
            raise ToolExecutionError("unsupported repository URL scheme", tool=self.name, url=url)
        target = context.resolve_path(destination) if destination else None
        args = ["clone"]
        if depth > 0:
            args.extend(["--depth", str(int(depth))])
        args.append(url)
        if target is not None:
            args.append(str(target))
        code, out, err = await _git(context, *args, timeout=max(context.timeout, 300.0))
        return _result(self.name, code, out, err, url=url, destination=str(target) if target else "")


class GitCommitTool(Tool):
    name = "git.commit"
    description = "Stage the given paths (or everything) and create a commit."
    risk = RiskLevel.HIGH
    required_permissions = frozenset({"git.write"})
    tags = frozenset({"git", "write"})
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "paths": {"type": "array", "description": "Workspace-relative paths to stage"},
            "allow_empty": {"type": "boolean"},
        },
        "required": ["message"],
    }

    async def run(
        self, context: ToolContext, message: str, paths: list[str] | None = None, allow_empty: bool = False
    ) -> ToolResult:
        text = str(message).strip()
        if not text:
            raise ToolExecutionError("commit message is required", tool=self.name)
        targets = [str(context.resolve_path(item)) for item in (paths or [])]
        code, out, err = await _git(context, "add", *(targets or ["-A"]))
        if code != 0:
            return _result(self.name, code, out, err, stage="add")
        args = ["commit", "-m", text]
        if allow_empty:
            args.append("--allow-empty")
        code, out, err = await _git(context, *args)
        result = _result(self.name, code, out, err, stage="commit", message=text)
        if code == 0:
            result.artifacts = [{"type": "commit", "name": text[:80], "content": out.strip()}]
        return result


class GitPushTool(Tool):
    name = "git.push"
    description = "Push commits to a remote. The highest-risk git operation."
    risk = RiskLevel.CRITICAL
    required_permissions = frozenset({"git.push", "network"})
    network = True
    tags = frozenset({"git", "network", "write"})
    parameters = {
        "type": "object",
        "properties": {"remote": {"type": "string"}, "branch": {"type": "string"}, "force": {"type": "boolean"}},
        "required": [],
    }

    async def run(
        self, context: ToolContext, remote: str = "origin", branch: str = "", force: bool = False
    ) -> ToolResult:
        args = ["push", remote]
        if branch:
            args.append(branch)
        if force:
            args.append("--force-with-lease")
        code, out, err = await _git(context, *args, timeout=max(context.timeout, 120.0))
        return _result(self.name, code, out, err, remote=remote, branch=branch, force=force)


def git_tools() -> list[Tool]:
    return [
        GitStatusTool(),
        GitDiffTool(),
        GitLogTool(),
        GitCloneTool(),
        GitCommitTool(),
        GitPushTool(),
    ]


__all__ = [
    "GitCloneTool",
    "GitCommitTool",
    "GitDiffTool",
    "GitLogTool",
    "GitPushTool",
    "GitStatusTool",
    "git_tools",
]
