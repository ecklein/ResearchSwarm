"""Shared validation used inside each backend's write lock."""

from collections.abc import Iterable

from researchswarm.research.graph import ResearchGraph
from researchswarm.research.models import Evidence, Experiment, ExperimentResult, ResearchEntity


def validate_save(
    existing: list[ResearchEntity], entity: ResearchEntity, *, create_only: bool
) -> None:
    previous = next((item for item in existing if item.id == entity.id), None)
    if previous is not None:
        if create_only:
            raise ValueError(f"duplicate research entity: {entity.id}")
        if type(previous) is not type(entity):
            raise ValueError("cannot change a research entity's type")
    ResearchGraph([item for item in existing if item.id != entity.id] + [entity])


def runtime_references(entity: ResearchEntity) -> Iterable[tuple[str, str]]:
    if isinstance(entity, Experiment):
        for task_id in entity.task_ids:
            yield "task", task_id
    elif isinstance(entity, Evidence) and entity.artifact_id is not None:
        yield "artifact", entity.artifact_id
    elif isinstance(entity, ExperimentResult):
        for artifact_id in entity.artifact_ids:
            yield "artifact", artifact_id
