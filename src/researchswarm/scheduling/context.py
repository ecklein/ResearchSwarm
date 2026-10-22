"""Optional contextual interfaces alongside the original scheduler protocol."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from researchswarm.models.task import Task
from researchswarm.scheduling.budget import BudgetSnapshot


@dataclass(frozen=True)
class SchedulingContext:
    run_id: str
    budget: BudgetSnapshot | None = None
    completed_task_ids: tuple[str, ...] = ()
    research_summary: Mapping[str, float | int] = field(default_factory=dict)


@runtime_checkable
class ContextualSchedulerProtocol(Protocol):
    def choose_with_context(
        self, ready_tasks: Sequence[Task], available_slots: int, context: SchedulingContext
    ) -> list[Task]: ...
