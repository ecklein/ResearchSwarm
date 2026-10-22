"""Deterministic information-value ranking and budget-aware admission."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Protocol, runtime_checkable

from researchswarm.models.task import Task, TaskStatus
from researchswarm.models.usage import nonnegative
from researchswarm.runtime.scheduler import PriorityScheduler, SchedulerProtocol
from researchswarm.scheduling.budget import BudgetLedger, affordable, reserve
from researchswarm.scheduling.context import ContextualSchedulerProtocol, SchedulingContext
from researchswarm.scheduling.signals import SchedulingSignals, get_scheduling_signals


@dataclass(frozen=True)
class TaskScore:
    task_id: str
    score: float
    expected_information_gain: float
    uncertainty: float
    novelty: float
    dependency_value: float
    estimated_cost: float
    estimated_tokens: int | None = None
    estimated_seconds: float | None = None
    basis: str = "information_value"


@runtime_checkable
class ExplainableSchedulerProtocol(Protocol):
    def score_tasks(self, ready_tasks: Sequence[Task]) -> list[TaskScore]: ...


class InformationValueScheduler:
    """Descending score, descending priority, ascending created_at, ascending ID.

    score = gain * uncertainty * novelty * dependency_value /
            max(estimated_cost, epsilon) ** alpha

    Missing signals use priority as the score by default, including in mixed queues.
    Set fallback_to_priority=False to score missing signals using default estimates.
    """

    def __init__(
        self,
        *,
        alpha: float = 1.0,
        epsilon: float = 1e-9,
        fallback_to_priority: bool = True,
    ) -> None:
        nonnegative(alpha, "alpha")
        nonnegative(epsilon, "epsilon")
        if epsilon == 0:
            raise ValueError("epsilon must be positive")
        self.alpha = alpha
        self.epsilon = epsilon
        self.fallback_to_priority = fallback_to_priority

    def score(self, task: Task) -> TaskScore:
        signals = get_scheduling_signals(task)
        fallback = signals is None and self.fallback_to_priority
        signals = signals or SchedulingSignals()
        if fallback:
            value = float(task.priority)
        else:
            numerator = (
                signals.expected_information_gain
                * signals.uncertainty
                * signals.novelty
                * signals.dependency_value
            )
            try:
                value = numerator / max(signals.estimated_cost, self.epsilon) ** self.alpha
            except (OverflowError, ZeroDivisionError) as exc:
                raise ValueError("score parameters exceed numeric range") from exc
        if not isfinite(value):
            raise ValueError("task score must be finite")
        return TaskScore(
            task.id, value, **asdict(signals), basis="priority" if fallback else "information_value"
        )

    def score_tasks(self, ready_tasks: Sequence[Task]) -> list[TaskScore]:
        candidates = [task for task in ready_tasks if task.status is TaskStatus.READY]
        scores = {task.id: self.score(task) for task in candidates}
        ordered = sorted(
            candidates,
            key=lambda task: (
                -scores[task.id].score,
                -task.priority,
                task.created_at,
                task.id,
            ),
        )
        return [scores[task.id] for task in ordered]

    def choose(self, ready_tasks: Sequence[Task], available_slots: int) -> list[Task]:
        if available_slots <= 0:
            return []
        tasks = {task.id: task for task in ready_tasks}
        return [tasks[score.task_id] for score in self.score_tasks(ready_tasks)[:available_slots]]

    def choose_with_context(
        self, ready_tasks: Sequence[Task], available_slots: int, context: SchedulingContext
    ) -> list[Task]:
        # Numeric signals are supplied externally; context never triggers text inference.
        return self.choose(ready_tasks, available_slots)


def choose_tasks(
    scheduler: SchedulerProtocol | ContextualSchedulerProtocol,
    ready_tasks: Sequence[Task],
    available_slots: int,
    context: SchedulingContext,
) -> list[Task]:
    if isinstance(scheduler, ContextualSchedulerProtocol):
        selected = scheduler.choose_with_context(ready_tasks, available_slots, context)
    else:
        selected = scheduler.choose(ready_tasks, available_slots)
    ids = [task.id for task in selected]
    candidates = {task.id: task for task in ready_tasks if task.status is TaskStatus.READY}
    if len(ids) > max(0, available_slots) or len(ids) != len(set(ids)):
        raise ValueError("scheduler must select unique tasks within available slots")
    if any(task_id not in candidates for task_id in ids):
        raise ValueError("scheduler selected a task outside the ready set")
    return [candidates[task_id] for task_id in ids]


class BudgetAwareScheduler:
    """Greedily admit affordable tasks in the wrapped policy's ranking order.

    Reservations cover the whole selected batch; skipped tasks stay READY. The
    wrapper requests a full ranking so an expensive top task cannot hide cheap work.
    A context budget takes precedence over the optional standalone ledger.
    """

    def __init__(
        self,
        scheduler: SchedulerProtocol | ContextualSchedulerProtocol | None = None,
        ledger: BudgetLedger | None = None,
    ) -> None:
        self.scheduler = scheduler or PriorityScheduler()
        self.ledger = ledger

    def choose(self, ready_tasks: Sequence[Task], available_slots: int) -> list[Task]:
        return self.choose_with_context(
            ready_tasks,
            available_slots,
            SchedulingContext(
                run_id="",
                budget=self.ledger.snapshot() if self.ledger else None,
            ),
        )

    def choose_with_context(
        self, ready_tasks: Sequence[Task], available_slots: int, context: SchedulingContext
    ) -> list[Task]:
        if available_slots <= 0:
            return []
        remaining = context.budget or (self.ledger.snapshot() if self.ledger else None)
        if remaining is None:
            return choose_tasks(self.scheduler, ready_tasks, available_slots, context)
        ranking = choose_tasks(self.scheduler, ready_tasks, len(ready_tasks), context)
        selected: list[Task] = []
        for task in ranking:
            signals = get_scheduling_signals(task) or SchedulingSignals()
            if affordable(signals, remaining):
                selected.append(task)
                remaining = reserve(signals, remaining)
                if len(selected) == available_slots:
                    break
        return selected

    def score_tasks(self, ready_tasks: Sequence[Task]) -> list[TaskScore]:
        if isinstance(self.scheduler, ExplainableSchedulerProtocol):
            return self.scheduler.score_tasks(ready_tasks)
        return []
