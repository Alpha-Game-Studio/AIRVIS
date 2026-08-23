"""Network tools. All of them declare ``network = True`` so they can be
disabled wholesale via ``security.allow_network``."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

from ..core.errors import ToolExecutionError
from .base import RiskLevel, Tool, ToolContext, ToolResult

MAX_FETCH_BYTES = 200_000
USER_AGENT = "AIRVIS/6.0"


def _require_http(url: str, tool: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolExecutionError("only http(s) URLs are allowed", tool=tool, url=url)
    return url


class WebFetchTool(Tool):
    name = "web.fetch"
    description = "Fetch a public HTTP(S) URL and return its decoded body."
    risk = RiskLevel.LOW
    required_permissions = frozenset({"network"})
    network = True
    tags = frozenset({"web", "read"})
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "max_bytes": {"type": "integer"}},
        "required": ["url"],
    }

    async def run(self, context: ToolContext, url: str, max_bytes: int = MAX_FETCH_BYTES) -> ToolResult:
        _require_http(url, self.name)
        limit = max(1, min(int(max_bytes), MAX_FETCH_BYTES))

        def _fetch() -> tuple[int, str, str]:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=context.timeout or 20.0) as response:
                body = response.read(limit)
                return response.status, response.headers.get("Content-Type", ""), body.decode("utf-8", errors="replace")

        status, content_type, text = await asyncio.to_thread(_fetch)
        return ToolResult(
            tool=self.name,
            ok=200 <= status < 400,
            output=text,
            error=None if status < 400 else f"HTTP {status}",
            metadata={"url": url, "status": status, "content_type": content_type},
        )


class BrowserOpenTool(Tool):
    name = "browser.open"
    description = "Open a URL in the desktop browser."
    risk = RiskLevel.LOW
    required_permissions = frozenset({"network"})
    network = True
    tags = frozenset({"web", "desktop"})
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    async def run(self, context: ToolContext, url: str) -> str:
        _require_http(url, self.name)
        await asyncio.to_thread(webbrowser.open, url)
        return f"opened {url}"


class GithubSearchTool(Tool):
    name = "github.search"
    description = "Search public GitHub repositories."
    risk = RiskLevel.LOW
    required_permissions = frozenset({"network"})
    network = True
    tags = frozenset({"web", "read"})
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    }

    async def run(self, context: ToolContext, query: str, limit: int = 10) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(str(query).strip())
        capped = max(1, min(int(limit), 50))
        url = f"https://api.github.com/search/repositories?q={encoded}&per_page={capped}"

        def _search() -> list[dict[str, Any]]:
            request = urllib.request.Request(
                url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(request, timeout=context.timeout or 20.0) as response:
                data = json.loads(response.read())
            return [
                {"name": item.get("full_name"), "url": item.get("html_url"), "description": item.get("description")}
                for item in data.get("items", [])
            ]

        return await asyncio.to_thread(_search)


def web_tools() -> list[Tool]:
    return [WebFetchTool(), BrowserOpenTool(), GithubSearchTool()]


__all__ = ["BrowserOpenTool", "GithubSearchTool", "WebFetchTool", "web_tools"]
