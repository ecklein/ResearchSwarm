"""Restricted runtime context passed to agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from researchswarm.models import Artifact, Event, Task
from researchswarm.storage.base import StorageProtocol

EventEmitter = Callable[[str, str | None, dict[str, Any]], Awaitable[Event]]


class RunContext:
    """Agent-facing gateway to persisted run state."""

    def __init__(
        self,
        run_id: str,
        task_id: str,
        storage: StorageProtocol,
        emit: EventEmitter,
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.storage = storage
        self._emit = emit

    async def get_task(self, task_id: str) -> Task:
        """Load a task in this run."""
        return await self.storage.get_task(self.run_id, task_id)

    async def get_artifact(self, artifact_id: str) -> Artifact:
        """Load an artifact in this run."""
        return await self.storage.get_artifact(self.run_id, artifact_id)

    async def get_dependency_results(self) -> dict[str, dict[str, Any]]:
        """Return structured outputs for all direct dependencies."""
        task = await self.get_task(self.task_id)
        results: dict[str, dict[str, Any]] = {}
        for dependency_id in task.dependencies:
            dependency = await self.get_task(dependency_id)
            results[dependency_id] = dependency.output
        return results

    async def list_artifacts(self) -> list[Artifact]:
        """List artifacts currently stored for this run."""
        return await self.storage.list_artifacts(self.run_id)

    async def emit_event(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> Event:
        """Append a custom event associated with the current task."""
        return await self._emit(event_type, self.task_id, payload or {})

