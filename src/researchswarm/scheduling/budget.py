"""Deterministic accounting and conservative batch admission by estimates."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal

from researchswarm.models.event import Event
from researchswarm.models.task import Task, TaskStatus
from researchswarm.models.usage import ResourceUsage, nonnegative
from researchswarm.scheduling.signals import SchedulingSignals


def _decimal(value: float | int) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class BudgetLimits:
    max_cost: float | None = None
    max_tokens: int | None = None
    max_tasks: int | None = None
    max_wall_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("max_cost", "max_tokens", "max_tasks", "max_wall_seconds"):
            value = getattr(self, name)
            if value is not None:
                nonnegative(value, name, integer=name in {"max_tokens", "max_tasks"})


@dataclass(frozen=True)
class BudgetSnapshot:
    cost_used: float = 0.0
    tokens_used: int = 0
    tasks_completed: int = 0
    wall_seconds_used: float = 0.0
    cost_remaining: float | None = None
    tokens_remaining: int | None = None
    tasks_remaining: int | None = None
    wall_seconds_remaining: float | None = None

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if value is not None:
                nonnegative(
                    value,
                    name,
                    integer=name
                    in {
                        "tokens_used",
                        "tasks_completed",
                        "tokens_remaining",
                        "tasks_remaining",
                    },
                )


def affordable(signals: SchedulingSignals, budget: BudgetSnapshot) -> bool:
    """Unknown token/time estimates consume no reservation; missing signals default to cost 1."""
    return all(
        remaining is None or _decimal(estimate) <= _decimal(remaining)
        for estimate, remaining in (
            (signals.estimated_cost, budget.cost_remaining),
            (signals.estimated_tokens or 0, budget.tokens_remaining),
            (1, budget.tasks_remaining),
            (signals.estimated_seconds or 0, budget.wall_seconds_remaining),
        )
    )


def reserve(signals: SchedulingSignals, budget: BudgetSnapshot) -> BudgetSnapshot:
    """Reserve additive cost/tokens/tasks within a concurrent batch.

    Duration estimates are checked individually, not summed: concurrent batch wall
    time is its maximum duration. Actual wall time is measured by the controller.
    """
    return replace(
        budget,
        cost_remaining=(
            None
            if budget.cost_remaining is None
            else float(_decimal(budget.cost_remaining) - _decimal(signals.estimated_cost))
        ),
        tokens_remaining=(
            None
            if budget.tokens_remaining is None
            else budget.tokens_remaining - (signals.estimated_tokens or 0)
        ),
        tasks_remaining=None if budget.tasks_remaining is None else budget.tasks_remaining - 1,
    )


def exhausted(snapshot: BudgetSnapshot) -> bool:
    return any(
        value is not None and value <= 0
        for value in (
            snapshot.cost_remaining,
            snapshot.tokens_remaining,
            snapshot.tasks_remaining,
            snapshot.wall_seconds_remaining,
        )
    )


class BudgetLedger:
    """Actual completed-task usage, deduplicated by task ID within a single run.

    Missing usage means unknown (accounted as zero), never estimated actual usage.
    Decimal arithmetic avoids accumulated binary rounding in scalar cost accounting.
    """

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self.limits = limits or BudgetLimits()
        self._cost = Decimal(0)
        self._tokens = 0
        self._completed: set[str] = set()
        self._wall_seconds = 0.0

    def record(self, task_id: str, usage: ResourceUsage | None = None) -> bool:
        if not task_id:
            raise ValueError("task_id must not be empty")
        if task_id in self._completed:
            return False
        actual = usage or ResourceUsage()
        actual.__post_init__()
        self._cost += _decimal(actual.cost)
        self._tokens += actual.input_tokens + actual.output_tokens
        self._completed.add(task_id)
        return True

    def observe_wall_seconds(self, seconds: float) -> None:
        nonnegative(seconds, "wall_seconds")
        self._wall_seconds = max(self._wall_seconds, seconds)

    def snapshot(self) -> BudgetSnapshot:
        limits = self.limits
        return BudgetSnapshot(
            cost_used=float(self._cost),
            tokens_used=self._tokens,
            tasks_completed=len(self._completed),
            wall_seconds_used=self._wall_seconds,
            cost_remaining=(
                None
                if limits.max_cost is None
                else float(max(Decimal(0), _decimal(limits.max_cost) - self._cost))
            ),
            tokens_remaining=(
                None if limits.max_tokens is None else max(0, limits.max_tokens - self._tokens)
            ),
            tasks_remaining=(
                None
                if limits.max_tasks is None
                else max(0, limits.max_tasks - len(self._completed))
            ),
            wall_seconds_remaining=(
                None
                if limits.max_wall_seconds is None
                else max(0.0, limits.max_wall_seconds - self._wall_seconds)
            ),
        )

    def can_afford(self, signals: SchedulingSignals) -> bool:
        return affordable(signals, self.snapshot())

    def exhausted(self) -> bool:
        return exhausted(self.snapshot())

    @classmethod
    def from_events(
        cls,
        events: Iterable[Event],
        limits: BudgetLimits | None = None,
        *,
        tasks: Iterable[Task] = (),
        run_id: str | None = None,
    ) -> BudgetLedger:
        """Replay one run, then fill completion-event gaps from saved terminal tasks.

        Pass run_id to filter a mixed trace; otherwise mixed runs are rejected.
        Task completion metadata provides recovery when a process died after saving
        the task but before appending its completion event. First record wins.
        """
        ledger = cls(limits)
        seen_runs: set[str] = set()
        for event in events:
            if run_id is not None and event.run_id != run_id:
                continue
            seen_runs.add(event.run_id)
            if len(seen_runs) > 1:
                raise ValueError("a budget ledger belongs to one run")
            if event.event_type == "task.completed" and event.task_id is not None:
                raw = event.payload.get("usage")
                ledger.record(event.task_id, ResourceUsage(**raw) if raw is not None else None)
            elif event.event_type == "budget.updated":
                ledger.observe_wall_seconds(event.payload.get("wall_seconds_used", 0.0))
        for task in tasks:
            if task.status is TaskStatus.COMPLETED:
                scoped = task.metadata.get("researchswarm", {})
                raw = scoped.get("usage") if isinstance(scoped, dict) else None
                ledger.record(task.id, ResourceUsage(**raw) if raw is not None else None)
        return ledger
