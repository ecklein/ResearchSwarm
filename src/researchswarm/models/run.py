"""Run models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from researchswarm.exceptions import InvalidStateTransitionError
from researchswarm.utils import ensure_aware, generate_id, utc_now

if TYPE_CHECKING:
    from researchswarm.storage.base import StorageProtocol


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Run:
    """Persistent metadata for one swarm execution."""

    objective: str
    id: str = field(default_factory=lambda: generate_id("run"))
    status: RunStatus = RunStatus.CREATED
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    root_task_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id or not self.objective:
            raise ValueError("run id and objective must not be empty")
        ensure_aware(self.created_at, "created_at")

    def transition_to(self, status: RunStatus, *, at: datetime | None = None) -> None:
        """Apply a validated run state transition."""
        valid = {
            RunStatus.CREATED: {RunStatus.RUNNING, RunStatus.CANCELLED},
            RunStatus.RUNNING: {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            },
            RunStatus.COMPLETED: set(),
            RunStatus.FAILED: set(),
            RunStatus.CANCELLED: set(),
        }
        if status not in valid[self.status]:
            raise InvalidStateTransitionError(
                f"run {self.id} cannot transition from {self.status.value} to {status.value}"
            )
        moment = at or utc_now()
        self.status = status
        if status is RunStatus.RUNNING:
            self.started_at = moment
        elif status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            self.completed_at = moment


@dataclass(frozen=True)
class SwarmResult:
    """Summary of a finished run with async access to its persisted records."""

    run_id: str
    status: RunStatus
    task_count: int
    completed_count: int
    failed_count: int
    blocked_count: int
    artifact_ids: list[str]
    started_at: datetime
    completed_at: datetime
    _storage: StorageProtocol = field(repr=False, compare=False)

    async def tasks(self):  # type: ignore[no-untyped-def]
        """Return all tasks in the run."""
        return await self._storage.list_tasks(self.run_id)

    async def artifacts(self):  # type: ignore[no-untyped-def]
        """Return all artifacts in the run."""
        return await self._storage.list_artifacts(self.run_id)

    async def events(self):  # type: ignore[no-untyped-def]
        """Return the ordered event trace for the run."""
        return await self._storage.list_events(self.run_id)
