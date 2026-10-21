"""Storage protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from researchswarm.models import Artifact, Event, Run, Task

if TYPE_CHECKING:
    from researchswarm.research.edges import ResearchEdge
    from researchswarm.research.models import ResearchEntity


class StorageProtocol(Protocol):
    """Minimal persistence contract used by the runtime."""

    async def save_run(self, run: Run) -> None: ...

    async def get_run(self, run_id: str) -> Run: ...

    async def save_task(self, run_id: str, task: Task) -> None: ...

    async def get_task(self, run_id: str, task_id: str) -> Task: ...

    async def list_tasks(self, run_id: str) -> list[Task]: ...

    async def save_artifact(self, run_id: str, artifact: Artifact) -> None: ...

    async def get_artifact(self, run_id: str, artifact_id: str) -> Artifact: ...

    async def list_artifacts(self, run_id: str) -> list[Artifact]: ...

    async def append_event(self, event: Event) -> Event: ...

    async def list_events(self, run_id: str) -> list[Event]: ...

    async def close(self) -> None: ...


@runtime_checkable
class ResearchStorageProtocol(StorageProtocol, Protocol):
    """Optional research persistence; generic agents need only StorageProtocol.

    Saves upsert within a run, preserving entity type. create_only rejects duplicates
    atomically. All references must already exist in the same run. Lists are ordered
    by creation time then ID; missing research IDs raise KeyError.
    """

    async def save_research_entity(
        self, run_id: str, entity: ResearchEntity, *, create_only: bool = False
    ) -> None: ...

    async def get_research_entity(self, run_id: str, entity_id: str) -> ResearchEntity: ...

    async def list_research_entities(
        self, run_id: str, entity_type: type[ResearchEntity] | None = None
    ) -> list[ResearchEntity]: ...

    async def save_research_edge(
        self, run_id: str, edge: ResearchEdge, *, create_only: bool = False
    ) -> None: ...

    async def list_research_edges(self, run_id: str) -> list[ResearchEdge]: ...

    async def delete_research_edge(self, run_id: str, edge_id: str) -> None: ...
