"""Runtime skill registry for AIRVIS orchestration.

A skill is a real, local capability package under ``~/.airvis/skills/<name>``.
Each package has ``manifest.json`` and may contain ``SKILL.md`` instructions.
Skills are loaded into agent context and never execute arbitrary Python code.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Skill:
    name: str
    description: str = ""
    instructions: str = ""
    tools: set[str] = field(default_factory=set)
    capabilities: set[str] = field(default_factory=set)
    enabled: bool = True
    path: Path | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tools": sorted(self.tools),
            "capabilities": sorted(self.capabilities),
            "enabled": self.enabled,
            "path": str(self.path) if self.path else None,
            "error": self.error,
        }


class SkillRegistry:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or Path.home() / ".airvis" / "skills"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.skills: dict[str, Skill] = {}
        self.discover()

    def discover(self) -> None:
        self.skills.clear()
        for manifest in sorted(self.directory.glob("*/manifest.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                name = str(data["name"])
                instructions_path = manifest.parent / "SKILL.md"
                instructions = instructions_path.read_text(encoding="utf-8") if instructions_path.is_file() else ""
                self.skills[name] = Skill(
                    name=name,
                    description=str(data.get("description", "")),
                    instructions=instructions,
                    tools=set(data.get("tools", [])),
                    capabilities=set(data.get("capabilities", [])),
                    enabled=bool(data.get("enabled", True)),
                    path=manifest.parent,
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.skills[manifest.parent.name] = Skill(
                    manifest.parent.name,
                    path=manifest.parent,
                    enabled=False,
                    error=str(exc),
                )

    def list(self) -> list[dict[str, Any]]:
        return [skill.to_dict() for skill in self.skills.values()]

    def prompt_context(self, capabilities: set[str] | None = None) -> str:
        capabilities = capabilities or set()
        selected = [
            skill for skill in self.skills.values()
            if skill.enabled and (not skill.capabilities or skill.capabilities & capabilities)
        ]
        if not selected:
            return ""
        blocks = ["## Installed AIRVIS Skills"]
        for skill in selected:
            if skill.instructions:
                blocks.append(f"### {skill.name}\n{skill.instructions}")
        return "\n\n".join(blocks)

    def create(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise ValueError("skill name must be a simple directory name")
        target = self.directory / name
        target.mkdir(parents=True, exist_ok=False)
        (target / "manifest.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "description": "",
                    "tools": [],
                    "capabilities": [],
                    "enabled": True,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        (target / "SKILL.md").write_text(
            f"# {name}\n\nDescribe the instructions this skill adds to AIRVIS agents.\n",
            encoding="utf-8",
        )
        self.discover()
        return target

    def enable(self, name: str, enabled: bool = True) -> bool:
        skill = self.skills.get(name)
        if not skill or not skill.path:
            return False
        manifest = skill.path / "manifest.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["enabled"] = enabled
            manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError):
            return False
        skill.enabled = enabled
        return True

    def remove(self, name: str) -> bool:
        skill = self.skills.get(name)
        if not skill or not skill.path or not skill.path.is_dir():
            return False
        shutil.rmtree(skill.path)
        del self.skills[name]
        return True


__all__ = ["Skill", "SkillRegistry"]
