"""Aggregates the built-in tool set installed into every registry."""

from __future__ import annotations

from .base import Tool
from .code import code_tools
from .filesystem import filesystem_tools
from .git import git_tools
from .terminal import terminal_tools
from .testing import testing_tools
from .web import web_tools


def builtin_tools() -> list[Tool]:
    """Return one fresh instance of every built-in tool."""
    return [
        *filesystem_tools(),
        *terminal_tools(),
        *git_tools(),
        *web_tools(),
        *code_tools(),
        *testing_tools(),
    ]


__all__ = ["builtin_tools"]
