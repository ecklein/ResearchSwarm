"""In-memory storage backend."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from researchswarm.exceptions import (
    ArtifactNotFoundError,
    RunNotFoundError,
    TaskNotFoundError,
)
from researchswarm.models import Artifact, Event, Run, Task
from researchswarm.research.edges import ResearchEdge
from researchswarm.research.models import ResearchEntity
from researchswarm.research.persistence import runtime_references, validate_save
from researchswarm.research.serialization import (
    edge_from_dict,
    edge_to_dict,
    entity_from_dict,
    entity_to_dict,
)
from researchswarm.serialization import (
    artifact_from_dict,
    event_from_dict,
    run_from_dict,
    task_from_dict,
    to_dict,
)


class MemoryStorage:
    """A concurrency-safe, process-local storage backend."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._tasks: dict[str, dict[str, Task]] = {}
        self._artifacts: dict[str, dict[str, Artifact]] = {}
        self._events: dict[str, list[Event]] = {}
        self._research_entities: dict[str, dict[str, ResearchEntity]] = {}
        self._research_edges: dict[str, dict[str, ResearchEdge]] = {}
        self._lock = asyncio.Lock()

    async def save_run(self, run: Run) -> None:
        async with self._lock:
            self._runs[run.id] = run_from_dict(to_dict(run))
            self._tasks.setdefault(run.id, {})
            self._artifacts.setdefault(run.id, {})
            self._events.setdefault(run.id, [])
            self._research_entities.setdefault(run.id, {})
            self._research_edges.setdefault(run.id, {})

    async def get_run(self, run_id: str) -> Run:
        async with self._lock:
            try:
                return run_from_dict(to_dict(self._runs[run_id]))
            except KeyError as exc:
                raise RunNotFoundError(f"run not found: {run_id}") from exc

    async def save_task(self, run_id: str, task: Task) -> None:
        async with self._lock:
            if run_id not in self._runs:
                raise RunNotFoundError(f"run not found: {run_id}")
            self._tasks[run_id][task.id] = task_from_dict(to_dict(task))

    async def get_task(self, run_id: str, task_id: str) -> Task:
        async with self._lock:
            try:
                return task_from_dict(to_dict(self._tasks[run_id][task_id]))
            except KeyError as exc:
                raise TaskNotFoundError(f"task not found: {task_id}") from exc

    async def list_tasks(self, run_id: str) -> list[Task]:
        async with self._lock:
            if run_id not in self._runs:
                raise RunNotFoundError(f"run not found: {run_id}")
            values = self._tasks[run_id].values()
            return [task_from_dict(to_dict(task)) for task in sorted(values, key=_task_key)]

    async def save_artifact(self, run_id: str, artifact: Artifact) -> None:
        async with self._lock:
            if run_id not in self._runs:
                raise RunNotFoundError(f"run not found: {run_id}")
            self._artifacts[run_id][artifact.id] = artifact_from_dict(to_dict(artifact))

    async def get_artifact(self, run_id: str, artifact_id: str) -> Artifact:
        async with self._lock:
            try:
                return artifact_from_dict(to_dict(self._artifacts[run_id][artifact_id]))
            except KeyError as exc:
                raise ArtifactNotFoundError(f"artifact not found: {artifact_id}") from exc

    async def list_artifacts(self, run_id: str) -> list[Artifact]:
        async with self._lock:
            if run_id not in self._runs:
                raise RunNotFoundError(f"run not found: {run_id}")
            values = self._artifacts[run_id].values()
            return [
                artifact_from_dict(to_dict(item))
                for item in sorted(values, key=lambda artifact: (artifact.created_at, artifact.id))
            ]

    async def append_event(self, event: Event) -> Event:
        async with self._lock:
            if event.run_id not in self._runs:
                raise RunNotFoundError(f"run not found: {event.run_id}")
            sequence = len(self._events[event.run_id]) + 1
            stored = replace(event, sequence_number=sequence)
            self._events[event.run_id].append(event_from_dict(to_dict(stored)))
            return stored

    async def list_events(self, run_id: str) -> list[Event]:
        async with self._lock:
            if run_id not in self._runs:
                raise RunNotFoundError(f"run not found: {run_id}")
            return [event_from_dict(to_dict(event)) for event in self._events[run_id]]

    def _require_research_run(self, run_id: str) -> None:
        if run_id not in self._runs:
            raise RunNotFoundError(f"run not found: {run_id}")

    async def save_research_entity(
        self, run_id: str, entity: ResearchEntity, *, create_only: bool = False
    ) -> None:
        copied = entity_from_dict(entity_to_dict(entity))
        async with self._lock:
            self._require_research_run(run_id)
            records = self._research_entities[run_id]
            validate_save(list(records.values()), copied, create_only=create_only)
            for kind, ref in runtime_references(copied):
                if kind == "task" and ref not in self._tasks[run_id]:
                    raise TaskNotFoundError(f"task not found: {ref}")
                if kind == "artifact" and ref not in self._artifacts[run_id]:
                    raise ArtifactNotFoundError(f"artifact not found: {ref}")
            records[copied.id] = copied

    async def get_research_entity(self, run_id: str, entity_id: str) -> ResearchEntity:
        async with self._lock:
            self._require_research_run(run_id)
            return entity_from_dict(entity_to_dict(self._research_entities[run_id][entity_id]))

    async def list_research_entities(
        self, run_id: str, entity_type: type[ResearchEntity] | None = None
    ) -> list[ResearchEntity]:
        async with self._lock:
            self._require_research_run(run_id)
            return [
                entity_from_dict(entity_to_dict(item))
                for item in sorted(
                    self._research_entities[run_id].values(),
                    key=lambda item: (item.created_at, item.id),
                )
                if entity_type is None or isinstance(item, entity_type)
            ]

    async def save_research_edge(
        self, run_id: str, edge: ResearchEdge, *, create_only: bool = False
    ) -> None:
        copied = edge_from_dict(edge_to_dict(edge))
        async with self._lock:
            self._require_research_run(run_id)
            records = self._research_edges[run_id]
            if create_only and copied.id in records:
                raise ValueError(f"duplicate research edge: {copied.id}")
            for ref in (copied.source_id, copied.target_id):
                if ref not in self._research_entities[run_id]:
                    raise KeyError(ref)
            records[copied.id] = copied

    async def list_research_edges(self, run_id: str) -> list[ResearchEdge]:
        async with self._lock:
            self._require_research_run(run_id)
            return [
                edge_from_dict(edge_to_dict(item))
                for item in sorted(
                    self._research_edges[run_id].values(),
                    key=lambda item: (item.created_at, item.id),
                )
            ]

    async def delete_research_edge(self, run_id: str, edge_id: str) -> None:
        async with self._lock:
            self._require_research_run(run_id)
            del self._research_edges[run_id][edge_id]

    async def close(self) -> None:
        """No-op for API compatibility."""


def _task_key(task: Task) -> tuple[object, str]:
    return task.created_at, task.id
