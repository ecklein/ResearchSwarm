"""Task model and task state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from researchswarm.exceptions import InvalidStateTransitionError
from researchswarm.utils import ensure_aware, generate_id, utc_now


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.BLOCKED}
    ),
    TaskStatus.READY: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.BLOCKED}
    ),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PENDING}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.BLOCKED: frozenset(),
}


@dataclass
class Task:
    """A unit of work in a dependency graph."""

    name: str
    description: str
    agent_type: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    priority: float = 0.0
    id: str = field(default_factory=lambda: generate_id("task"))
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("task id and name must not be empty")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("task dependencies must be unique")
        if self.id in self.dependencies:
            raise ValueError("a task cannot depend on itself")
        ensure_aware(self.created_at, "created_at")
        for field_name, value in (
            ("started_at", self.started_at),
            ("completed_at", self.completed_at),
        ):
            if value is not None:
                ensure_aware(value, field_name)

    def transition_to(self, status: TaskStatus, *, at: datetime | None = None) -> None:
        """Apply a validated state transition and update timestamps."""
        if status not in _TASK_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError(
                f"task {self.id} cannot transition from {self.status.value} to {status.value}"
            )
        moment = at or utc_now()
        ensure_aware(moment, "transition time")
        self.status = status
        if status is TaskStatus.RUNNING:
            self.started_at = moment
        elif status is TaskStatus.PENDING:  # interrupted task recovery
            self.started_at = None
            self.completed_at = None
            self.error = None
        elif status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        }:
            self.completed_at = moment
