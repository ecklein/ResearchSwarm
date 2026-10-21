"""Structured execution event model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from researchswarm.utils import ensure_aware, generate_id, utc_now


@dataclass(frozen=True)
class Event:
    """An immutable record in a run's event trace."""

    run_id: str
    event_type: str
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: generate_id("event"))
    timestamp: datetime = field(default_factory=utc_now)
    sequence_number: int | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.run_id or not self.event_type:
            raise ValueError("event id, run_id, and event_type must not be empty")
        ensure_aware(self.timestamp, "timestamp")

