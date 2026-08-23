"""Shell execution with risk classification.

The tool never bypasses the permission system: the declared risk is HIGH, and
:func:`classify_command` can raise it to CRITICAL for destructive commands.
"""

from __future__ import annotations

import asyncio
import shlex

from ..core.errors import PermissionDeniedError, ToolExecutionError
from .base import RiskLevel, Tool, ToolContext, ToolResult

MAX_OUTPUT_CHARS = 40_000

#: Substrings that mark a command as CRITICAL regardless of policy.
CRITICAL_PATTERNS = (
    "rm -rf /", "mkfs", "dd if=", ":(){", "shutdown", "reboot", "halt",
    "diskutil erase", "chown -r /", "chmod -r 777 /", "> /dev/sd",
)
#: Commands that always require the HIGH tier.
HIGH_PATTERNS = ("sudo", "rm ", "rmdir", "mv ", "chmod", "chown", "git push", "kill ", "pkill", "curl ", "wget ")
MEDIUM_PATTERNS = ("pip install", "npm install", "git commit", "git add", "make", "docker", "apt", "brew")
#: Read-only commands that are safe enough to auto-approve.
SAFE_COMMANDS = {
    "pwd", "ls", "whoami", "date", "uname", "env", "id", "df", "free",
    "git status", "git diff", "git log", "git branch", "python --version",
    "python3 --version", "pip --version", "node --version",
}


def classify_command(command: str) -> RiskLevel:
    """Map a shell command onto the AIRVIS risk scale."""
    lowered = " ".join(command.lower().split())
    if not lowered:
        return RiskLevel.SAFE
    if any(pattern in lowered for pattern in CRITICAL_PATTERNS):
        return RiskLevel.CRITICAL
    if lowered in SAFE_COMMANDS or any(lowered.startswith(f"{item} ") for item in SAFE_COMMANDS):
        return RiskLevel.SAFE
    if any(pattern in lowered for pattern in HIGH_PATTERNS):
        return RiskLevel.HIGH
    if any(pattern in lowered for pattern in MEDIUM_PATTERNS):
        return RiskLevel.MEDIUM
    return RiskLevel.MEDIUM


class TerminalExecuteTool(Tool):
    name = "terminal.execute"
    description = "Run a shell command in the workspace and capture its output."
    risk = RiskLevel.HIGH
    required_permissions = frozenset({"terminal.execute"})
    tags = frozenset({"terminal", "execute"})
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number"},
            "cwd": {"type": "string", "description": "Workspace-relative working directory"},
        },
        "required": ["command"],
    }

    async def run(
        self, context: ToolContext, command: str, timeout: float | None = None, cwd: str | None = None
    ) -> ToolResult:
        text = str(command).strip()
        if not text:
            raise ToolExecutionError("command is required", tool=self.name)

        level = classify_command(text)
        if level is RiskLevel.CRITICAL:
            # Escalation beyond the declared risk of the tool: refuse outright so
            # a CRITICAL command can never inherit a HIGH approval.
            raise PermissionDeniedError(
                f"CRITICAL command blocked: {text}", tool=self.name, risk=RiskLevel.CRITICAL.name
            )

        try:
            argv = shlex.split(text)
        except ValueError as exc:
            raise ToolExecutionError(f"cannot parse command: {exc}", tool=self.name) from exc
        if not argv:
            raise ToolExecutionError("command is empty after parsing", tool=self.name)

        workdir = context.resolve_path(cwd) if cwd else context.workspace
        limit = float(timeout or context.timeout or 60.0)

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=limit)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        except FileNotFoundError as exc:  # pragma: no cover - platform dependent
            raise ToolExecutionError(f"command not found: {argv[0]}", tool=self.name) from exc

        out = stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
        err = stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
        code = process.returncode or 0
        return ToolResult(
            tool=self.name,
            ok=code == 0,
            output=(out or err).strip(),
            error=None if code == 0 else (err.strip() or f"exit code {code}"),
            metadata={"exit_code": code, "risk": level.name, "command": text, "stdout": out, "stderr": err},
        )


def terminal_tools() -> list[Tool]:
    return [TerminalExecuteTool()]


__all__ = ["TerminalExecuteTool", "classify_command", "terminal_tools"]
