"""A research graph permits cycles and never schedules or infers anything."""

from collections.abc import Iterable
from typing import TypeVar, overload

from researchswarm.research.edges import ResearchEdge, ResearchRelation
from researchswarm.research.models import (
    Claim,
    Conflict,
    Critique,
    Evidence,
    Experiment,
    ExperimentResult,
    Hypothesis,
    ResearchEntity,
)
from researchswarm.research.serialization import (
    edge_from_dict,
    edge_to_dict,
    entity_from_dict,
    entity_to_dict,
)

T = TypeVar("T", bound=ResearchEntity)


class ResearchGraph:
    """An isolated snapshot with deterministic ordering and validated references.

    Missing IDs raise KeyError; duplicate IDs and invalid reference types raise
    ValueError. Reads return copies so callers cannot invalidate graph structure.
    Embedded research references are validated but do not create implicit edges.
    """

    def __init__(
        self, entities: Iterable[ResearchEntity] = (), edges: Iterable[ResearchEdge] = ()
    ) -> None:
        self._entities: dict[str, ResearchEntity] = {}
        self._edges: dict[str, ResearchEdge] = {}
        for entity in entities:
            if entity.id in self._entities:
                raise ValueError(f"duplicate research entity: {entity.id}")
            self._entities[entity.id] = entity_from_dict(entity_to_dict(entity))
        for entity in self._entities.values():
            self._validate_references(entity)
        for edge in edges:
            self.add_edge(edge)

    def _validate_references(self, entity: ResearchEntity) -> None:
        if isinstance(entity, Experiment):
            for ref in entity.hypothesis_ids:
                if not isinstance(self.get(ref), Hypothesis):
                    raise ValueError(f"expected hypothesis: {ref}")
        elif isinstance(entity, ExperimentResult):
            if not isinstance(self.get(entity.experiment_id), Experiment):
                raise ValueError(f"expected experiment: {entity.experiment_id}")
        elif isinstance(entity, Critique):
            self.get(entity.target_id)
        elif isinstance(entity, Conflict):
            self.get(entity.left_id)
            self.get(entity.right_id)

    def add(self, entity: ResearchEntity) -> None:
        if entity.id in self._entities:
            raise ValueError(f"duplicate research entity: {entity.id}")
        copied = entity_from_dict(entity_to_dict(entity))
        self._validate_references(copied)
        self._entities[copied.id] = copied

    def get(self, entity_id: str) -> ResearchEntity:
        return entity_from_dict(entity_to_dict(self._entities[entity_id]))

    @overload
    def entities(self, entity_type: type[T]) -> list[T]: ...

    @overload
    def entities(self, entity_type: None = None) -> list[ResearchEntity]: ...

    def entities(self, entity_type: type[T] | None = None) -> list[T] | list[ResearchEntity]:
        return [
            self.get(entity.id)
            for entity in sorted(
                self._entities.values(), key=lambda item: (item.created_at, item.id)
            )
            if entity_type is None or isinstance(entity, entity_type)
        ]

    def add_edge(self, edge: ResearchEdge) -> None:
        if edge.id in self._edges:
            raise ValueError(f"duplicate research edge: {edge.id}")
        copied = edge_from_dict(edge_to_dict(edge))
        self.get(copied.source_id)
        self.get(copied.target_id)
        self._edges[copied.id] = copied

    def remove_edge(self, edge_id: str) -> None:
        del self._edges[edge_id]

    def edges(self) -> list[ResearchEdge]:
        return [
            edge_from_dict(edge_to_dict(edge))
            for edge in sorted(self._edges.values(), key=lambda item: (item.created_at, item.id))
        ]

    def incoming(self, entity_id: str) -> list[ResearchEdge]:
        self.get(entity_id)
        return [edge for edge in self.edges() if edge.target_id == entity_id]

    def outgoing(self, entity_id: str) -> list[ResearchEdge]:
        self.get(entity_id)
        return [edge for edge in self.edges() if edge.source_id == entity_id]

    def _select(self, ids: set[str]) -> list[ResearchEntity]:
        return [entity for entity in self.entities() if entity.id in ids]

    def parents(self, entity_id: str) -> list[ResearchEntity]:
        """Direct sources of incoming edges, deduplicated across relations."""
        return self._select({edge.source_id for edge in self.incoming(entity_id)})

    def children(self, entity_id: str) -> list[ResearchEntity]:
        """Direct targets of outgoing edges."""
        return self._select({edge.target_id for edge in self.outgoing(entity_id)})

    def related(self, entity_id: str) -> list[ResearchEntity]:
        """Direct neighbors in either direction, not transitive closure."""
        return self._select(
            {item.id for item in self.parents(entity_id) + self.children(entity_id)}
        )

    def _sources(self, entity_id: str, relation: ResearchRelation, cls: type[T]) -> list[T]:
        ids = {edge.source_id for edge in self.incoming(entity_id) if edge.relation == relation}
        return [item for item in self.entities() if item.id in ids and isinstance(item, cls)]

    def evidence_for(self, entity_id: str) -> list[Evidence]:
        return self._sources(entity_id, ResearchRelation.SUPPORTS, Evidence)

    def evidence_against(self, entity_id: str) -> list[Evidence]:
        return self._sources(entity_id, ResearchRelation.CONTRADICTS, Evidence)

    def experiments_for(self, hypothesis_id: str) -> list[Experiment]:
        linked = {
            item.id for item in self._sources(hypothesis_id, ResearchRelation.TESTS, Experiment)
        }
        return [
            item
            for item in self.entities()
            if isinstance(item, Experiment)
            and (item.id in linked or hypothesis_id in item.hypothesis_ids)
        ]

    def claims_for(self, hypothesis_id: str) -> list[Claim]:
        """Claims explicitly supporting or contradicting the hypothesis."""
        ids = {
            edge.source_id
            for edge in self.incoming(hypothesis_id)
            if edge.relation in (ResearchRelation.SUPPORTS, ResearchRelation.CONTRADICTS)
        }
        return [item for item in self.entities() if isinstance(item, Claim) and item.id in ids]

    def critiques_for(self, entity_id: str) -> list[Critique]:
        linked = {
            item.id for item in self._sources(entity_id, ResearchRelation.CRITIQUES, Critique)
        }
        return [
            item
            for item in self.entities()
            if isinstance(item, Critique) and (item.id in linked or item.target_id == entity_id)
        ]

    def conflicts_for(self, entity_id: str) -> list[Conflict]:
        self.get(entity_id)
        return [
            item
            for item in self.entities()
            if isinstance(item, Conflict) and entity_id in (item.left_id, item.right_id)
        ]
