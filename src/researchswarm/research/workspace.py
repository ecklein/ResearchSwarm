"""An async, run-scoped gateway to persistent research state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeVar

from researchswarm.research.edges import ResearchEdge, ResearchRelation
from researchswarm.research.graph import ResearchGraph
from researchswarm.research.models import (
    Claim,
    Conflict,
    ConflictStatus,
    Critique,
    Evidence,
    Experiment,
    ExperimentResult,
    Hypothesis,
    ResearchEntity,
    entity_type,
)
from researchswarm.research.serialization import entity_from_dict, entity_to_dict

if TYPE_CHECKING:
    from researchswarm.runtime.context import RunContext
    from researchswarm.storage.base import ResearchStorageProtocol

T = TypeVar("T", bound=ResearchEntity)
ResearchEmitter = Callable[[str, dict[str, Any]], Awaitable[object]]


class ResearchWorkspace:
    """Research writes are explicit and persisted before optional event emission.

    Emitters receive (event_type, payload). State and events are separate writes,
    as in the runtime: an emitter failure does not roll back persisted state.
    A run must already exist. Returned records and graphs are detached snapshots.
    """

    def __init__(
        self,
        run_id: str,
        storage: ResearchStorageProtocol,
        emit: ResearchEmitter | None = None,
    ) -> None:
        from researchswarm.storage.base import ResearchStorageProtocol

        if not isinstance(storage, ResearchStorageProtocol):
            raise TypeError("storage must implement ResearchStorageProtocol")
        self.run_id = run_id
        self.storage = storage
        self._emit = emit

    @classmethod
    def from_context(cls, context: RunContext) -> ResearchWorkspace:
        """Opt in without changing RunContext or ordinary agents."""
        from researchswarm.storage.base import ResearchStorageProtocol

        if not isinstance(context.storage, ResearchStorageProtocol):
            raise TypeError("context storage does not support research persistence")
        return cls(context.run_id, context.storage, context.emit_event)

    async def _event(self, name: str, payload: dict[str, Any]) -> None:
        if self._emit is not None:
            await self._emit(name, payload)

    async def _entity_event(self, entity: ResearchEntity, action: str) -> None:
        kind = entity_type(entity)
        payload: dict[str, Any] = {
            "entity_id": entity.id,
            "entity_type": kind,
            f"{kind}_id": entity.id,
        }
        if hasattr(entity, "status"):
            payload["status"] = entity.status.value
        name = f"research.{kind}.{action}"
        if isinstance(entity, ExperimentResult):
            payload["experiment_id"] = entity.experiment_id
            name = f"research.experiment.result_{action}"
        await self._event(name, payload)

    async def _add(self, entity: T) -> T:
        # Round trip revalidates mutable dataclasses before persistence.
        snapshot = entity_from_dict(entity_to_dict(entity))
        await self.storage.save_research_entity(self.run_id, snapshot, create_only=True)
        await self._entity_event(snapshot, "created")
        return entity  # The backend stores its own detached copy.

    async def update(self, entity: T) -> T:
        """Explicitly replace an existing entity, without automatic status transitions."""
        previous = await self.get(entity.id)
        snapshot = entity_from_dict(entity_to_dict(entity))
        if type(previous) is not type(snapshot):
            raise ValueError("cannot change a research entity's type")
        await self.storage.save_research_entity(self.run_id, snapshot)
        action = "updated"
        if (
            isinstance(snapshot, Conflict)
            and isinstance(previous, Conflict)
            and snapshot.status == ConflictStatus.RESOLVED
            and previous.status != ConflictStatus.RESOLVED
        ):
            action = "resolved"
        await self._entity_event(snapshot, action)
        return entity

    async def resolve_conflict(self, conflict_id: str) -> Conflict:
        conflict = await self.get(conflict_id)
        if not isinstance(conflict, Conflict):
            raise ValueError(f"expected conflict: {conflict_id}")
        return await self.update(replace(conflict, status=ConflictStatus.RESOLVED))

    async def link(
        self,
        source: str | ResearchEntity,
        target: str | ResearchEntity,
        relation: ResearchRelation | str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ResearchEdge:
        edge = ResearchEdge(
            source if isinstance(source, str) else source.id,
            target if isinstance(target, str) else target.id,
            relation,
            metadata={} if metadata is None else metadata,
        )
        await self.storage.save_research_edge(self.run_id, edge, create_only=True)
        await self._event(
            "research.edge.created",
            {
                "edge_id": edge.id,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "relation": str(edge.relation),
            },
        )
        return edge

    async def remove_edge(self, edge_id: str) -> None:
        await self.storage.delete_research_edge(self.run_id, edge_id)
        await self._event("research.edge.deleted", {"edge_id": edge_id})

    async def graph(self) -> ResearchGraph:
        # Read edges first: concurrent additions cannot introduce dangling endpoints
        # into this view. This is a detached view, not a transaction across both reads.
        edges = await self.storage.list_research_edges(self.run_id)
        entities = await self.storage.list_research_entities(self.run_id)
        return ResearchGraph(entities, edges)

    async def get(self, entity_id: str) -> ResearchEntity:
        return await self.storage.get_research_entity(self.run_id, entity_id)

    async def add_hypothesis(self, entity: Hypothesis) -> Hypothesis:
        return await self._add(entity)

    async def add_claim(self, entity: Claim) -> Claim:
        return await self._add(entity)

    async def add_evidence(self, entity: Evidence) -> Evidence:
        return await self._add(entity)

    async def add_experiment(self, entity: Experiment) -> Experiment:
        return await self._add(entity)

    async def add_result(self, entity: ExperimentResult) -> ExperimentResult:
        return await self._add(entity)

    async def add_critique(self, entity: Critique) -> Critique:
        return await self._add(entity)

    async def add_conflict(self, entity: Conflict) -> Conflict:
        return await self._add(entity)

    async def hypotheses(self) -> list[Hypothesis]:
        items = await self.storage.list_research_entities(self.run_id, Hypothesis)
        return [item for item in items if isinstance(item, Hypothesis)]

    async def claims(self) -> list[Claim]:
        items = await self.storage.list_research_entities(self.run_id, Claim)
        return [item for item in items if isinstance(item, Claim)]

    async def experiments(self) -> list[Experiment]:
        items = await self.storage.list_research_entities(self.run_id, Experiment)
        return [item for item in items if isinstance(item, Experiment)]
