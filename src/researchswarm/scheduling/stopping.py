"""Explicit, composable stopping decisions evaluated between execution rounds."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from researchswarm.models.task import Task
from researchswarm.models.usage import nonnegative
from researchswarm.scheduling.budget import BudgetSnapshot, affordable, exhausted
from researchswarm.scheduling.signals import SchedulingSignals, get_scheduling_signals


@dataclass(frozen=True)
class OrchestrationState:
    run_id: str
    ready_tasks: tuple[Task, ...] = ()
    completed_task_ids: tuple[str, ...] = ()
    budget: BudgetSnapshot | None = None


@dataclass(frozen=True)
class StopDecision:
    stop: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.stop and not self.reason:
            raise ValueError("a stop decision requires a reason")


class StopConditionProtocol(Protocol):
    def should_stop(self, state: OrchestrationState) -> StopDecision: ...


@runtime_checkable
class SlotLimitProtocol(Protocol):
    """Optional admission cap prevents a task-count stop overshooting in a batch."""

    def limit_slots(self, state: OrchestrationState, slots: int) -> int: ...


class BudgetExhaustedStop:
    def should_stop(self, state: OrchestrationState) -> StopDecision:
        if state.budget is not None and exhausted(state.budget):
            return StopDecision(True, "budget_exhausted")
        return StopDecision()


class MaxTasksStop:
    def __init__(self, max_tasks: int) -> None:
        nonnegative(max_tasks, "max_tasks", integer=True)
        self.max_tasks = max_tasks

    def should_stop(self, state: OrchestrationState) -> StopDecision:
        if len(state.completed_task_ids) >= self.max_tasks:
            return StopDecision(True, "max_tasks")
        return StopDecision()

    def limit_slots(self, state: OrchestrationState, slots: int) -> int:
        return max(0, min(slots, self.max_tasks - len(state.completed_task_ids)))


class NoAffordableTasksStop:
    def should_stop(self, state: OrchestrationState) -> StopDecision:
        if (
            state.budget is not None
            and state.ready_tasks
            and not any(
                affordable(get_scheduling_signals(task) or SchedulingSignals(), state.budget)
                for task in state.ready_tasks
            )
        ):
            return StopDecision(True, "no_affordable_tasks")
        return StopDecision()


class AnyStopCondition:
    """Return the first matching decision, in configured order."""

    def __init__(self, conditions: Sequence[StopConditionProtocol]) -> None:
        self.conditions = tuple(conditions)

    def should_stop(self, state: OrchestrationState) -> StopDecision:
        for condition in self.conditions:
            decision = condition.should_stop(state)
            if decision.stop:
                return decision
        return StopDecision()

    def limit_slots(self, state: OrchestrationState, slots: int) -> int:
        for condition in self.conditions:
            if isinstance(condition, SlotLimitProtocol):
                slots = min(slots, condition.limit_slots(state, slots))
        return max(0, slots)
