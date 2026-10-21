"""Directed research relationships, independent of execution dependencies."""

from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from researchswarm.research.models import _text
from researchswarm.utils import ensure_aware, generate_id, utc_now


class ResearchRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    TESTS = "tests"
    PRODUCES = "produces"
    CRITIQUES = "critiques"
    DERIVED_FROM = "derived_from"
    REFINES = "refines"
    RESOLVES = "resolves"


@dataclass
class ResearchEdge:
    source_id: str
    target_id: str
    # Custom nonempty strings allow domain-specific relations without changing the enum.
    relation: ResearchRelation | str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    id: str = field(default_factory=lambda: generate_id("edge"))

    def __post_init__(self) -> None:
        for name in ("id", "source_id", "target_id", "relation"):
            _text(getattr(self, name), name)
        ensure_aware(self.created_at, "created_at")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dictionary")
        with suppress(ValueError):
            self.relation = ResearchRelation(self.relation)
