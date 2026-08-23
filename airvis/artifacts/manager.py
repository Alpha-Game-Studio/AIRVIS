"""First-class artifacts.

Tasks reference artifacts by id instead of copying large payloads into the
context window; the manager owns the content and its versions.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..core.errors import ArtifactError
from ..core.events import EventBus, EventType

INLINE_LIMIT = 64_000
PREVIEW_CHARS = 600


class ArtifactType(str, Enum):
    FILE = "file"
    PATCH = "patch"
    REPORT = "report"
    IMAGE = "image"
    JSON = "json"
    TEST_RESULT = "test_result"
    ANALYSIS = "analysis"
    LOG = "log"
    COMMIT = "commit"
    TEXT = "text"

    @classmethod
    def parse(cls, value: ArtifactType | str | None) -> ArtifactType:
        if isinstance(value, ArtifactType):
            return value
        token = str(value or "text").strip().lower()
        try:
            return cls(token)
        except ValueError:
            return cls.TEXT


@dataclass
class Artifact:
    type: ArtifactType
    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    path: str | None = None
    content: Any = None
    creator: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    created_at: float = field(default_factory=time.time)
    version: int = 1
    parent_id: str | None = None
    size: int = 0
    digest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def preview(self, limit: int = PREVIEW_CHARS) -> str:
        if self.content is None:
            return f"<{self.type.value} at {self.path or 'unknown location'}>"
        text = self.content if isinstance(self.content, str) else json.dumps(self.content, ensure_ascii=False)
        return text[:limit] + ("…" if len(text) > limit else "")

    def reference(self) -> dict[str, Any]:
        """Compact descriptor safe to inline into a prompt."""
        return {
            "id": self.id,
            "type": self.type.value,
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "size": self.size,
            "task_id": self.task_id,
        }

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        payload = {
            **self.reference(),
            "creator": self.creator,
            "workflow_id": self.workflow_id,
            "created_at": self.created_at,
            "parent_id": self.parent_id,
            "digest": self.digest,
            "metadata": self.metadata,
            "preview": self.preview(),
        }
        if include_content:
            payload["content"] = self.content
        return payload


class ArtifactManager:
    """In-memory index with optional spill-to-disk for large payloads."""

    def __init__(self, root: Path | str | None = None, *, event_bus: EventBus | None = None) -> None:
        self.root = Path(root).expanduser() if root else Path.home() / ".airvis" / "artifacts"
        self.event_bus = event_bus
        self._lock = threading.RLock()
        self._artifacts: dict[str, Artifact] = {}

    # -- creation --------------------------------------------------------------

    def create(
        self,
        type: ArtifactType | str,
        name: str,
        *,
        content: Any = None,
        path: str | Path | None = None,
        creator: str | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        # NB: the ``type`` parameter shadows the builtin, so never call ``type()`` here.
        artifact_type = ArtifactType.parse(type)
        serialised = content if isinstance(content, str) or content is None else json.dumps(
            content, ensure_ascii=False, default=str
        )
        size = len(serialised.encode("utf-8")) if isinstance(serialised, str) else 0
        digest = hashlib.sha256(serialised.encode("utf-8")).hexdigest()[:32] if isinstance(serialised, str) else ""

        version = 1
        if parent_id:
            parent = self._artifacts.get(parent_id)
            if parent is None:
                raise ArtifactError(f"unknown parent artifact: {parent_id}", artifact=parent_id)
            version = parent.version + 1

        artifact = Artifact(
            type=artifact_type,
            name=name or artifact_type.value,
            path=str(path) if path else None,
            content=content,
            creator=creator,
            task_id=task_id,
            workflow_id=workflow_id,
            version=version,
            parent_id=parent_id,
            size=size,
            digest=digest,
            metadata=dict(metadata or {}),
        )

        if isinstance(serialised, str) and size > INLINE_LIMIT and artifact.path is None:
            artifact.path = str(self._spill(artifact, serialised))
            artifact.content = None

        with self._lock:
            self._artifacts[artifact.id] = artifact

        if self.event_bus is not None:
            self.event_bus.publish(
                EventType.ARTIFACT_CREATED,
                workflow_id=workflow_id,
                task_id=task_id,
                agent_id=creator,
                status="created",
                metadata=artifact.reference(),
            )
        return artifact

    def from_tool_result(
        self,
        descriptors: builtins.list[dict[str, Any]],
        *,
        creator: str | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
    ) -> builtins.list[Artifact]:
        created: builtins.list[Artifact] = []
        for descriptor in descriptors or []:
            if not isinstance(descriptor, dict):
                continue
            created.append(
                self.create(
                    descriptor.get("type", "text"),
                    str(descriptor.get("name") or "artifact"),
                    content=descriptor.get("content"),
                    path=descriptor.get("path"),
                    creator=creator,
                    task_id=task_id,
                    workflow_id=workflow_id,
                    metadata=dict(descriptor.get("metadata") or {}),
                )
            )
        return created

    def new_version(self, artifact_id: str, content: Any, *, metadata: dict[str, Any] | None = None) -> Artifact:
        parent = self.get(artifact_id)
        return self.create(
            parent.type,
            parent.name,
            content=content,
            path=parent.path,
            creator=parent.creator,
            task_id=parent.task_id,
            workflow_id=parent.workflow_id,
            parent_id=parent.id,
            metadata={**parent.metadata, **(metadata or {})},
        )

    # -- lookup ----------------------------------------------------------------

    def get(self, artifact_id: str) -> Artifact:
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            raise ArtifactError(f"unknown artifact: {artifact_id}", artifact=artifact_id)
        return artifact

    def read(self, artifact_id: str) -> Any:
        artifact = self.get(artifact_id)
        if artifact.content is not None:
            return artifact.content
        if artifact.path and Path(artifact.path).is_file():
            return Path(artifact.path).read_text(encoding="utf-8", errors="replace")
        return None

    def list(
        self,
        *,
        workflow_id: str | None = None,
        task_id: str | None = None,
        type: ArtifactType | str | None = None,
        latest_only: bool = False,
    ) -> builtins.list[Artifact]:
        with self._lock:
            items = list(self._artifacts.values())
        if workflow_id:
            items = [item for item in items if item.workflow_id == workflow_id]
        if task_id:
            items = [item for item in items if item.task_id == task_id]
        if type is not None:
            wanted = ArtifactType.parse(type)
            items = [item for item in items if item.type is wanted]
        items.sort(key=lambda item: (item.created_at, item.version))
        if latest_only:
            superseded = {item.parent_id for item in items if item.parent_id}
            items = [item for item in items if item.id not in superseded]
        return items

    def references(self, *, workflow_id: str | None = None, task_id: str | None = None) -> builtins.list[dict[str, Any]]:
        return [artifact.reference() for artifact in self.list(workflow_id=workflow_id, task_id=task_id)]

    def clear(self) -> None:
        with self._lock:
            self._artifacts.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._artifacts)

    def _spill(self, artifact: Artifact, payload: str) -> Path:
        directory = self.root / (artifact.workflow_id or "shared")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{artifact.id}-{_safe_name(artifact.name)}"
        target.write_text(payload, encoding="utf-8")
        return target


def _safe_name(name: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "-._" else "-" for character in name)
    return cleaned[:60] or "artifact"


__all__ = ["Artifact", "ArtifactManager", "ArtifactType"]
