from __future__ import annotations

import builtins
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Plugin:
    name: str
    version: str = "0.1.0"
    description: str = ""
    permissions: set[str] = field(default_factory=set)
    enabled: bool = True


class PluginManager:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or Path.home() / ".airvis" / "plugins"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.plugins: dict[str, Plugin] = {}
        self.discover()

    def discover(self) -> None:
        for manifest in self.directory.glob("*/manifest.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                self.plugins[data["name"]] = Plugin(
                    data["name"], data.get("version", "0.1.0"), data.get("description", ""), set(data.get("permissions", [])), data.get("enabled", True)
                )
            except (OSError, ValueError, KeyError):
                continue

    def list(self) -> builtins.list[dict[str, object]]:
        return [{"name": plugin.name, "version": plugin.version, "permissions": sorted(plugin.permissions), "enabled": plugin.enabled} for plugin in self.plugins.values()]

    def enable(self, name: str, enabled: bool = True) -> bool:
        if name not in self.plugins:
            return False
        self.plugins[name].enabled = enabled
        return True

    def create(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise ValueError("plugin name must be a simple directory name")
        target = self.directory / name
        target.mkdir(parents=True, exist_ok=False)
        (target / "manifest.json").write_text(json.dumps({"name": name, "version": "0.1.0", "permissions": [], "tools": []}, indent=2), encoding="utf-8")
        (target / "plugin.py").write_text(
            f"from airvis.sdk import Plugin\n\nPLUGIN = Plugin({name!r})\n", encoding="utf-8"
        )
        self.plugins[name] = Plugin(name)
        return target

    def remove(self, name: str) -> bool:
        target = self.directory / name
        if name not in self.plugins or not target.is_dir():
            return False
        for child in target.iterdir():
            if child.is_file():
                child.unlink()
        target.rmdir()
        del self.plugins[name]
        return True

    def validate_permissions(self, name: str, requested: set[str]) -> bool:
        plugin = self.plugins.get(name)
        return bool(plugin and requested.issubset(plugin.permissions))
