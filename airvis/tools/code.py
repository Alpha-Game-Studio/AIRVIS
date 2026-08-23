"""Static analysis tools.

``code.analyze`` performs real AST analysis instead of asking a model to guess:
findings are reproducible and can be verified by the review system.
"""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import ToolExecutionError
from .base import RiskLevel, Tool, ToolContext, ToolResult

MAX_FILES = 400
_IGNORED_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", "build", "dist"}


@dataclass
class Finding:
    file: str
    line: int
    rule: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass
class _ModuleReport:
    path: str
    findings: list[Finding] = field(default_factory=list)


class _Analyzer(ast.NodeVisitor):
    """Collects defect patterns that are decidable from the syntax tree alone."""

    def __init__(self, relative_path: str, module: ast.Module) -> None:
        self.path = relative_path
        self.findings: list[Finding] = []
        self._module_assigned = _module_level_bindings(module)
        self._function_stack: list[str] = []

    # -- structural checks -----------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function(node)

    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_stack.append(node.name)
        self._check_unreachable(node.body)
        self._check_globals(node)
        self._check_mutable_defaults(node)
        self.generic_visit(node)
        self._function_stack.pop()

    def _check_unreachable(self, body: list[ast.stmt]) -> None:
        for index, statement in enumerate(body):
            if isinstance(statement, (ast.Return, ast.Raise, ast.Continue, ast.Break)) and index + 1 < len(body):
                nxt = body[index + 1]
                self.findings.append(
                    Finding(
                        self.path,
                        nxt.lineno,
                        "unreachable-code",
                        "high",
                        f"statement is unreachable: it follows a {type(statement).__name__.lower()} "
                        f"on line {statement.lineno}",
                    )
                )
                break

    def _check_globals(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """``global x`` where ``x`` is never bound at module level raises NameError."""
        declared: set[str] = set()
        assigned_in_function: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Global):
                declared.update(child.names)
            elif isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                assigned_in_function.update(_assigned_names(child))
        for name in sorted(declared - self._module_assigned):
            severity = "high" if name not in assigned_in_function else "medium"
            self.findings.append(
                Finding(
                    self.path,
                    node.lineno,
                    "global-without-module-binding",
                    severity,
                    f"'{node.name}' declares 'global {name}' but {name} is never bound at module level; "
                    "reading it raises NameError",
                )
            )

    @staticmethod
    def _is_mutable(default: ast.expr) -> bool:
        return isinstance(default, (ast.List, ast.Dict, ast.Set)) or (
            isinstance(default, ast.Call)
            and isinstance(default.func, ast.Name)
            and default.func.id in {"list", "dict", "set"}
        )

    def _check_mutable_defaults(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for default in [*node.args.defaults, *(item for item in node.args.kw_defaults if item is not None)]:
            if self._is_mutable(default):
                self.findings.append(
                    Finding(
                        self.path, default.lineno, "mutable-default-argument", "medium",
                        f"'{node.name}' uses a mutable default argument; it is shared across calls",
                    )
                )

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.findings.append(
                Finding(self.path, node.lineno, "bare-except", "medium", "bare 'except:' swallows every error")
            )
        elif (
            isinstance(node.type, ast.Name)
            and node.type.id == "Exception"
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Pass)
        ):
            self.findings.append(
                Finding(
                    self.path, node.lineno, "silent-exception", "high",
                    "'except Exception: pass' silently swallows failures",
                )
            )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(comparator, (ast.Constant,)) and isinstance(
                comparator.value, (int, str, bytes)
            ):
                self.findings.append(
                    Finding(
                        self.path, node.lineno, "identity-comparison-literal", "medium",
                        "'is' compares identity, not value; use '==' for literals",
                    )
                )
        self.generic_visit(node)


def _module_level_bindings(module: ast.Module) -> set[str]:
    names: set[str] = set()
    for statement in module.body:
        if isinstance(statement, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            names.update(_assigned_names(statement))
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(statement.name)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(statement, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
            for child in ast.walk(statement):
                if isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    names.update(_assigned_names(child))
    return names


def _assigned_names(node: ast.stmt) -> set[str]:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    names: set[str] = set()
    for target in targets:
        for child in ast.walk(target):
            if isinstance(child, ast.Name):
                names.add(child.id)
    return names


def analyze_source(source: str, relative_path: str) -> list[Finding]:
    """Analyse one Python module; syntax errors become findings themselves."""
    try:
        module = ast.parse(source, filename=relative_path)
    except SyntaxError as exc:
        return [Finding(relative_path, exc.lineno or 1, "syntax-error", "critical", str(exc.msg))]
    analyzer = _Analyzer(relative_path, module)
    analyzer.visit(module)
    analyzer._check_unreachable(module.body)
    return sorted(analyzer.findings, key=lambda item: (item.file, item.line, item.rule))


class CodeAnalyzeTool(Tool):
    name = "code.analyze"
    description = "Statically analyse Python sources in the workspace and report defects."
    risk = RiskLevel.SAFE
    tags = frozenset({"code", "read", "analysis"})
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory, defaults to the workspace"},
            "pattern": {"type": "string"},
            "min_severity": {"type": "string", "description": "low | medium | high | critical"},
            "limit": {"type": "integer"},
        },
        "required": [],
    }

    async def run(
        self,
        context: ToolContext,
        path: str = "",
        pattern: str = "**/*.py",
        min_severity: str = "low",
        limit: int = 200,
    ) -> ToolResult:
        root = context.resolve_path(path) if path else context.workspace
        if not root.exists():
            raise ToolExecutionError(f"path does not exist: {path}", tool=self.name, path=path)
        ranking = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        threshold = ranking.get(str(min_severity).lower(), 0)
        capped = max(1, min(int(limit), 1000))

        def _analyze() -> tuple[list[Finding], int]:
            files = [root] if root.is_file() else _collect(root, pattern)
            findings: list[Finding] = []
            for file in files:
                try:
                    source = file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                relative = str(file.relative_to(context.workspace)) if file != context.workspace else file.name
                findings.extend(analyze_source(source, relative))
            filtered = [item for item in findings if ranking.get(item.severity, 0) >= threshold]
            return filtered[:capped], len(files)

        findings, scanned = await asyncio.to_thread(_analyze)
        payload = [item.to_dict() for item in findings]
        summary = f"{len(findings)} finding(s) across {scanned} file(s)"
        return ToolResult(
            tool=self.name,
            ok=True,
            output={"summary": summary, "findings": payload, "files_scanned": scanned},
            metadata={"files_scanned": scanned, "finding_count": len(findings)},
            artifacts=[{"type": "analysis", "name": "code-analysis", "content": payload}] if payload else [],
        )


def _collect(root: Path, pattern: str) -> list[Path]:
    files: list[Path] = []
    for candidate in root.rglob(pattern):
        if not candidate.is_file() or any(part in _IGNORED_PARTS for part in candidate.parts):
            continue
        files.append(candidate)
        if len(files) >= MAX_FILES:
            break
    return sorted(files)


def code_tools() -> list[Tool]:
    return [CodeAnalyzeTool()]


__all__ = ["CodeAnalyzeTool", "Finding", "analyze_source", "code_tools"]
