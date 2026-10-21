"""Artifact model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from researchswarm.utils import ensure_aware, generate_id, utc_now


@dataclass
class Artifact:
    """A durable output produced by an agent."""

    name: str
    artifact_type: str
    data: Any | None = None
    uri: str | None = None
    producer_task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: generate_id("artifact"))
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.id or not self.name or not self.artifact_type:
            raise ValueError("artifact id, name, and artifact_type must not be empty")
        ensure_aware(self.created_at, "created_at")

