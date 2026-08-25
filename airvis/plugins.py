"""Runtime plugin manager for AIRVIS.

Plugins are real Python extensions. A plugin directory contains ``manifest.json``
and ``plugin.py``. ``plugin.py`` must expose ``register(context)`` (or a
``PLUGIN`` object with ``register(context)``). Registration is performed only
for enabled plugins and all tool registration still goes through ToolRegistry.
"""
from __future__ import annotations

import builtins
import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any


@dataclass
class Plugin:
    name: str
    version: str = "0.1.0"
    description: str = ""
    permissions: set[str] = field(default_factory=set)
    enabled: bool = True
    tools: list[str] = field(default_factory=list)
    module: ModuleType | None = field(default=None, repr=False)
    loaded: bool = False
    error: str | None = None


class PluginContext:
    """Capabilities intentionally exposed to a plugin at registration time."""

    def __init__(self, *, plugin: Plugin, tools: Any, providers: Any, event_bus: Any = None) -> None:
        self.plugin = plugin
        self.tools = tools
        self.providers = providers
        self.event_bus = event_bus

    def register_tool(self, tool: Any) -> Any:
        return self.tools.register(tool)

    def require_permissions(self, requested: set[str]) -> None:
        if not requested.issubset(self.plugin.permissions):
            missing = sorted(requested - self.plugin.permissions)
            raise PermissionError(f"plugin {self.plugin.name!r} lacks permissions: {', '.join(missing)}")


class PluginManager:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or Path.home() / ".airvis" / "plugins"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.plugins: dict[str, Plugin] = {}
        self.discover()

    def discover(self) -> None:
        self.plugins.clear()
        for manifest in sorted(self.directory.glob("*/manifest.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                name = str(data["name"])
                self.plugins[name] = Plugin(
                    name=name,
                    version=str(data.get("version", "0.1.0")),
                    description=str(data.get("description", "")),
                    permissions=set(data.get("permissions", [])),
                    enabled=bool(data.get("enabled", True)),
                    tools=list(data.get("tools", [])),
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def load_enabled(self, *, tools: Any, providers: Any, event_bus: Any = None) -> list[dict[str, Any]]:
        """Import and register enabled plugins. Failures are reported, never hidden."""
        results: list[dict[str, Any]] = []
        for plugin in self.plugins.values():
            if not plugin.enabled:
                continue
            path = self.directory / plugin.name / "plugin.py"
            if not path.is_file():
                plugin.error = "plugin.py is missing"
                results.append({"name": plugin.name, "loaded": False, "error": plugin.error})
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"airvis_plugin_{plugin.name}", path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not create module spec")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                context = PluginContext(plugin=plugin, tools=tools, providers=providers, event_bus=event_bus)
                register = getattr(module, "register", None)
                if callable(register):
                    register(context)
                else:
                    obj = getattr(module, "PLUGIN", None)
                    register = getattr(obj, "register", None)
                    if not callable(register):
                        raise TypeError("plugin.py must expose register(context) or PLUGIN.register(context)")
                    register(context)
                plugin.module = module
                plugin.loaded = True
                plugin.error = None
                results.append({"name": plugin.name, "loaded": True})
            except Exception as exc:
                plugin.loaded = False
                plugin.error = f"{type(exc).__name__}: {exc}"
                results.append({"name": plugin.name, "loaded": False, "error": plugin.error})
        return results

    def list(self) -> builtins.list[dict[str, object]]:
        return [
            {"name": p.name, "version": p.version, "description": p.description,
             "permissions": sorted(p.permissions), "enabled": p.enabled,
             "loaded": p.loaded, "error": p.error}
            for p in self.plugins.values()
        ]

    def enable(self, name: str, enabled: bool = True) -> bool:
        if name not in self.plugins:
            return False
        self.plugins[name].enabled = enabled
        manifest = self.directory / name / "manifest.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["enabled"] = enabled
            manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError):
            return False
        return True

    def create(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise ValueError("plugin name must be a simple directory name")
        target = self.directory / name
        target.mkdir(parents=True, exist_ok=False)
        (target / "manifest.json").write_text(json.dumps({"name": name, "version": "0.1.0", "permissions": [], "tools": [], "enabled": True}, indent=2) + "\n", encoding="utf-8")
        (target / "plugin.py").write_text(
            """\n\ndef register(context):\n    # Register real AIRVIS Tool/Provider/Agent extensions here.\n    return None\n""".lstrip(), encoding="utf-8")
        self.plugins[name] = Plugin(name)
        return target

    def remove(self, name: str) -> bool:
        target = self.directory / name
        if name not in self.plugins or not target.is_dir():
            return False
        for child in target.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        target.rmdir()
        del self.plugins[name]
        return True

    def validate_permissions(self, name: str, requested: set[str]) -> bool:
        plugin = self.plugins.get(name)
        return bool(plugin and requested.issubset(plugin.permissions))
