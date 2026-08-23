"""Environment and configuration diagnostics.

``airvis doctor`` answers one question: *is this installation able to run a
workflow end to end, and if not, exactly what is broken?*
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from typing import Any

from .core.asyncutil import run_blocking

REQUIRED_PACKAGES = ("numpy", "dotenv", "websockets")
OPTIONAL_PACKAGES = ("yaml", "pytest", "pytest_asyncio")
EXTERNAL_BINARIES = ("ollama", "openclaw", "hermes", "git")


def _check(name: str, ok: bool, detail: str = "", severity: str = "error") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "severity": "info" if ok else severity}


def run_checks(engine: Any = None) -> list[dict[str, Any]]:
    """Run every diagnostic. Building the engine is itself one of the checks."""
    checks: list[dict[str, Any]] = [
        _check("python", sys.version_info >= (3, 10), sys.version.split()[0]),
    ]
    for package in REQUIRED_PACKAGES:
        checks.append(
            _check(f"package:{package}", importlib.util.find_spec(package) is not None, severity="warning")
        )
    for package in OPTIONAL_PACKAGES:
        found = importlib.util.find_spec(package) is not None
        checks.append(_check(f"optional:{package}", found, "" if found else "not installed", "info"))
    for binary in EXTERNAL_BINARIES:
        path = shutil.which(binary)
        checks.append(_check(f"binary:{binary}", path is not None, path or "not on PATH", "info"))

    owns_engine = engine is None
    if owns_engine:
        try:
            from .engine import AirvisEngine

            engine = AirvisEngine()
        except Exception as exc:
            checks.append(_check("engine", False, f"{type(exc).__name__}: {exc}"))
            return checks
    checks.append(_check("engine", True, f"config from {engine.config.source}"))

    # -- configuration ---------------------------------------------------------
    try:
        engine.config.routing.resolved_weights()
        checks.append(_check("config:routing", True, engine.config.routing.strategy))
    except Exception as exc:
        checks.append(_check("config:routing", False, str(exc)))

    # -- registries ------------------------------------------------------------
    checks.append(_check("providers", len(engine.providers) > 0, ", ".join(engine.providers.names())))
    checks.append(_check("backends", len(engine.backends) > 0, ", ".join(engine.backends.names())))
    checks.append(_check("tools", len(engine.tools) > 0, f"{len(engine.tools)} registered"))
    checks.append(_check("agents", len(engine.agents) > 0, ", ".join(engine.agents.names())))

    # -- agent reference integrity --------------------------------------------
    broken = {
        agent.id: problems
        for agent in engine.agents.all(enabled_only=False)
        if (problems := engine.agents.reference_problems(agent))
    }
    checks.append(
        _check(
            "agents:references",
            not broken,
            "; ".join(f"{agent}: {', '.join(items)}" for agent, items in broken.items()) or "all references valid",
        )
    )

    # -- tool registrations ----------------------------------------------------
    invalid = [
        tool.name
        for tool in engine.tools
        if not tool.description or not isinstance(tool.parameters, dict)
    ]
    checks.append(_check("tools:registrations", not invalid, ", ".join(invalid) or "all tools well-formed"))

    # -- live health -----------------------------------------------------------
    try:
        report = run_blocking(engine.health_check())
    except Exception as exc:
        checks.append(_check("health", False, f"{type(exc).__name__}: {exc}", "warning"))
        return checks

    for provider_id, status in report.get("providers", {}).items():
        state = status.get("state")
        checks.append(
            _check(
                f"provider:{provider_id}",
                state != "unhealthy",
                f"{state}: {status.get('detail', '')}",
                "warning",
            )
        )
    for backend_id, status in report.get("backends", {}).items():
        state = status.get("state")
        checks.append(
            _check(
                f"backend:{backend_id}",
                state != "unhealthy",
                f"{state}: {status.get('detail', '')}",
                "warning",
            )
        )

    usable = [
        provider_id
        for provider_id, status in report.get("providers", {}).items()
        if status.get("state") != "unhealthy"
    ]
    checks.append(_check("providers:usable", bool(usable), ", ".join(usable) or "no provider can serve a request"))

    # -- test environment ------------------------------------------------------
    has_pytest = importlib.util.find_spec("pytest") is not None
    has_async = importlib.util.find_spec("pytest_asyncio") is not None
    checks.append(
        _check(
            "tests:environment",
            has_pytest and has_async,
            "pytest + pytest-asyncio available"
            if has_pytest and has_async
            else "install: pip install pytest pytest-asyncio",
            "warning",
        )
    )
    return checks


def summarize(checks: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [item for item in checks if not item["ok"] and item["severity"] == "error"]
    warnings = [item for item in checks if not item["ok"] and item["severity"] == "warning"]
    return {
        "ok": not errors,
        "errors": len(errors),
        "warnings": len(warnings),
        "checks": checks,
    }


__all__ = ["run_checks", "summarize"]
