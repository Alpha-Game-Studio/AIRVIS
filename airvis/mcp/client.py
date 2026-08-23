"""Minimal MCP stdio client (JSON-RPC 2.0 over a subprocess pipe)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from ..core.errors import BackendUnavailableError, ToolExecutionError

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "airvis", "version": "6.0.0"}


class MCPClient:
    """Talks to one MCP server over stdio."""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------------

    async def connect(self) -> dict[str, Any]:
        if not self.command:
            raise BackendUnavailableError(f"MCP server '{self.name}' has no command configured", server=self.name)
        environment = {**os.environ, **self.env}
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except (OSError, FileNotFoundError) as exc:
            raise BackendUnavailableError(
                f"cannot start MCP server '{self.name}': {exc}", server=self.name
            ) from exc

        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": CLIENT_INFO,
            },
        )
        await self._notify("notifications/initialized", {})
        return result

    async def close(self) -> None:
        if self._process is None:
            return
        process, self._process = self._process, None
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (ProcessLookupError, asyncio.TimeoutError, OSError):  # pragma: no cover - shutdown races
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    # -- MCP API ---------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return [item for item in (tools or []) if isinstance(item, dict) and item.get("name")]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            raise ToolExecutionError(f"MCP tool '{name}' reported an error", tool=name, detail=str(result))
        blocks = result.get("content") or []
        texts = [str(block.get("text", "")) for block in blocks if isinstance(block, dict) and "text" in block]
        return "\n".join(texts) if texts else result

    # -- transport -------------------------------------------------------------

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        async with self._lock:
            process = self._require_process()
            self._next_id += 1
            message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
            await self._write(process, message)
            while True:
                payload = await self._read(process)
                if payload.get("id") != self._next_id:
                    continue  # notification or an out-of-band message
                if "error" in payload:
                    error = payload["error"]
                    raise ToolExecutionError(
                        f"MCP '{self.name}' {method} failed: {error.get('message', error)}", server=self.name
                    )
                return payload.get("result", {})

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        process = self._require_process()
        await self._write(process, {"jsonrpc": "2.0", "method": method, "params": params})

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise BackendUnavailableError(f"MCP server '{self.name}' is not connected", server=self.name)
        return self._process

    async def _write(self, process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def _read(self, process: asyncio.subprocess.Process) -> dict[str, Any]:
        assert process.stdout is not None
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=self.timeout)
            if not line:
                raise BackendUnavailableError(f"MCP server '{self.name}' closed the connection", server=self.name)
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue  # servers may emit log lines on stdout
            if isinstance(payload, dict):
                return payload


__all__ = ["MCPClient"]
