"""External agent runtimes driven over their command line interface.

OpenClaw and Hermes are *backends*, not providers: they own their own agent
loop, tools and session handling. AIRVIS decides what should happen and hands
the instruction over; the backend decides how the agent executes.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from ..core.errors import BackendError, BackendTimeoutError, BackendUnavailableError
from ..core.health import HealthState, HealthStatus
from .base import Backend, BackendType, ExecutionRequest, ExecutionResult

MAX_OUTPUT_CHARS = 60_000
SEARCH_DIRECTORIES = (
    Path.home() / ".hermes" / "bin",
    Path.home() / ".local" / "bin",
    Path.home() / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
)


def find_binary(names: list[str]) -> str | None:
    """Locate an executable on PATH or in the usual per-user install roots."""
    for name in names:
        if not name:
            continue
        found = shutil.which(name)
        if found:
            return found
        candidate_path = Path(name)
        if candidate_path.is_file() and os.access(candidate_path, os.X_OK):
            return str(candidate_path)
        for directory in SEARCH_DIRECTORIES:
            candidate = directory / candidate_path.name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


class CLIBackend(Backend):
    """Runs an external agent binary as a subprocess."""

    type = BackendType.CUSTOM
    capabilities = frozenset({"chat", "tools", "cancel", "sessions"})

    #: candidate executable names, first match wins
    binaries: tuple[str, ...] = ()
    #: argv template; ``{message}`` and ``{model}`` are substituted
    argv_templates: tuple[tuple[str, ...], ...] = ()

    def __init__(
        self,
        id: str,
        command: str = "",
        *,
        workspace: Path | str | None = None,
        timeout: float = 180.0,
        **overrides: Any,
    ) -> None:
        self.id = id
        self.command = command
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.timeout = timeout
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        super().__init__(**overrides)

    # -- binary resolution -----------------------------------------------------

    def resolve_binary(self) -> str | None:
        return find_binary([self.command, *self.binaries])

    def _argv_variants(self, message: str, model: str) -> list[list[str]]:
        binary = self.resolve_binary()
        if binary is None:
            return []
        variants: list[list[str]] = []
        for template in self.argv_templates:
            argv = [binary]
            skip = False
            for token in template:
                if token == "{model}":
                    if not model:
                        skip = True
                        break
                    argv.append(model)
                elif token == "{message}":
                    argv.append(message)
                else:
                    argv.append(token)
            if not skip:
                variants.append(argv)
        return variants

    # -- execution -------------------------------------------------------------

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        binary = self.resolve_binary()
        if binary is None:
            raise BackendUnavailableError(
                f"{self.id} CLI not found (looked for: {', '.join([self.command, *self.binaries])})",
                backend=self.id,
            )

        started = time.perf_counter()
        model = request.model or request.agent.model or ""
        message = self._compose_instruction(request)
        variants = self._argv_variants(message, model)
        failures: list[str] = []

        for argv in variants:
            try:
                code, stdout, stderr = await self._spawn(argv, request)
            except BackendTimeoutError:
                raise
            except OSError as exc:
                failures.append(f"{argv[1] if len(argv) > 1 else argv[0]}: {exc}")
                continue
            if code == 0 and stdout.strip():
                text = _extract_text(stdout)
                return ExecutionResult(
                    ok=True,
                    output=text,
                    backend_id=self.id,
                    provider_id=request.provider_id or request.agent.provider_id,
                    model=model or None,
                    execution_id=request.execution_id,
                    iterations=1,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    metadata={"argv": argv[1:], "exit_code": code},
                )
            failures.append(f"exit {code}: {(stderr or stdout).strip()[:300]}")

        raise BackendError(
            f"{self.id} CLI produced no usable output: " + " | ".join(failures[:3]),
            backend=self.id,
            attempts=len(variants),
        )

    async def _spawn(self, argv: list[str], request: ExecutionRequest) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes[request.execution_id] = process
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=min(request.timeout or self.timeout, self.timeout)
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise BackendTimeoutError(
                f"{self.id} CLI timed out after {self.timeout}s", backend=self.id
            ) from exc
        finally:
            self._processes.pop(request.execution_id, None)
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS],
            stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS],
        )

    def _compose_instruction(self, request: ExecutionRequest) -> str:
        """External runtimes get the instruction plus a compact context digest."""
        parts = [request.instruction]
        bundle = request.context
        if bundle is not None:
            if bundle.previous_results:
                digest = "\n".join(
                    f"- {item.get('name') or item.get('task_id')}: {str(item.get('output', ''))[:400]}"
                    for item in bundle.previous_results[-3:]
                )
                parts.append(f"[선행 결과]\n{digest}")
            if bundle.review_notes:
                parts.append("[리뷰 지적사항]\n" + "\n".join(f"- {note}" for note in bundle.review_notes))
        return "\n\n".join(part for part in parts if part)

    async def cancel(self, execution_id: str) -> bool:
        process = self._processes.get(execution_id)
        if process is None:
            return False
        process.kill()
        return True

    async def health_check(self) -> HealthStatus:
        binary = self.resolve_binary()
        if binary is None:
            return HealthStatus(
                HealthState.UNHEALTHY, f"{self.id} CLI is not installed", time.time()
            )
        return HealthStatus(HealthState.HEALTHY, f"found at {binary}", time.time())

    def describe(self) -> dict[str, Any]:
        payload = super().describe()
        payload["binary"] = self.resolve_binary()
        payload["command"] = self.command
        return payload


class OpenClawBackend(CLIBackend):
    """OpenClaw gateway agent."""

    type = BackendType.OPENCLAW
    description = "OpenClaw local gateway agent (desktop automation and tools)."
    binaries = ("openclaw",)
    argv_templates = (
        ("agent", "--message", "{message}", "--model", "{model}", "--json"),
        ("agent", "--message", "{message}", "--json"),
        ("agent", "--message", "{message}"),
    )

    def __init__(self, command: str = "openclaw", **overrides: Any) -> None:
        super().__init__("openclaw", command, **overrides)


class HermesBackend(CLIBackend):
    """Hermes Agent (Nous Research) CLI."""

    type = BackendType.HERMES
    description = "Hermes Agent runtime by Nous Research."
    binaries = ("hermes", "hermes-agent")
    argv_templates = (
        ("chat", "--message", "{message}", "--model", "{model}"),
        ("chat", "--message", "{message}"),
        ("--message", "{message}"),
        ("run", "{message}"),
    )

    def __init__(self, command: str = "hermes", **overrides: Any) -> None:
        super().__init__("hermes", command, **overrides)


def _extract_text(raw: str) -> str:
    """CLI agents emit either plain text or a JSON envelope."""
    text = raw.strip()
    if not text.startswith(("{", "[")):
        return text
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    return _dig(payload) or text


def _dig(payload: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("text", "content", "message", "response", "output", "result", "answer", "data"):
            if key in payload:
                found = _dig(payload[key], depth + 1)
                if found:
                    return found
        return ""
    if isinstance(payload, list):
        for item in reversed(payload):
            found = _dig(item, depth + 1)
            if found:
                return found
    return ""


__all__ = ["CLIBackend", "HermesBackend", "OpenClawBackend", "find_binary"]
