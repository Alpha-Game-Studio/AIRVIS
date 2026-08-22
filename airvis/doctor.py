from __future__ import annotations

import importlib.util
import shutil
import sys
from typing import Any


def run_checks() -> list[dict[str, Any]]:
    checks = [{"name": "python", "ok": sys.version_info >= (3, 10), "detail": sys.version.split()[0]}]
    for package in ("numpy", "dotenv", "websockets"):
        checks.append({"name": package, "ok": importlib.util.find_spec(package) is not None})
    checks.append({"name": "ollama", "ok": shutil.which("ollama") is not None})
    checks.append({"name": "openclaw", "ok": shutil.which("openclaw") is not None})
    checks.append({"name": "hermes", "ok": shutil.which("hermes") is not None})
    return checks
