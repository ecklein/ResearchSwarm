"""Deterministic event reduction. Replay never executes an agent or reads storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from researchswarm.models.event import Event
from researchswarm.models.run import RunStatus
from researchswarm.models.task import TaskStatus
from researchswarm.models.usage import ResourceUsage
from researchswarm.scheduling.budget import BudgetSnapshot
from researchswarm.trace import codec
from researchswarm.trace.common import ValidationResult, check_version
from researchswarm.trace.models import UsageSummary
from researchswarm.trace.trace import RunTrace

TASK_EVENTS = {
    "task.created": TaskStatus.PENDING,
    "task.ready": TaskStatus.READY,
    "task.started": TaskStatus.RUNNING,
    "task.completed": TaskStatus.COMPLETED,
    "task.failed": TaskStatus.FAILED,
    "task.blocked": TaskStatus.BLOCKED,
    "task.cancelled": TaskStatus.CANCELLED,
    "task.recovered": TaskStatus.PENDING,
}
RUN_EVENTS = {
    "run.created": RunStatus.CREATED,
    "run.started": RunStatus.RUNNING,
    "run.resumed": RunStatus.RUNNING,
    "run.completed": RunStatus.COMPLETED,
    "run.failed": RunStatus.FAILED,
    "run.cancelled": RunStatus.CANCELLED,
}


def sequence_errors(events: tuple[Event, ...]) -> list[str]:
    values = [event.sequence_number for event in events]
    if any(type(value) is not int or value < 1 for value in values):
        return ["event sequence numbers must be positive integers"]
    numbers = [value for value in values if isinstance(value, int)]
    errors = []
    if len(values) != len(set(values)):
        errors.append("duplicate event sequence numbers")
    if sorted(numbers) != list(range(1, len(values) + 1)):
        errors.append("event sequence numbers must be contiguous starting at 1")
    if values != sorted(numbers):
        errors.append("events are not in sequence-number order")
    return errors


@dataclass
class ReplayState:
    run_status: RunStatus = RunStatus.CREATED
    task_statuses: dict[str, TaskStatus] = field(default_factory=dict)
    task_execution_order: list[str] = field(default_factory=list)
    artifacts_created: list[str] = field(default_factory=list)
    task_usage: dict[str, ResourceUsage | None] = field(default_factory=dict)
    budget: BudgetSnapshot | None = None
    stop_reason: str | None = None
    research_entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    research_edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    research_events: list[Event] = field(default_factory=list)
    uninterpreted_events: list[Event] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def usage(self) -> UsageSummary:
        return UsageSummary.aggregate(self.task_usage.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_status": self.run_status,
            "task_statuses": self.task_statuses,
            "task_execution_order": self.task_execution_order,
            "artifacts_created": self.artifacts_created,
            "usage": codec.summary_dict(self.usage),
            "stop_reason": self.stop_reason,
            "research_entities": self.research_entities,
            "research_edges": self.research_edges,
            "uninterpreted_event_ids": [event.id for event in self.uninterpreted_events],
            "diagnostics": self.diagnostics,
            "budget": (
                {
                    name: getattr(self.budget, name)
                    for name in (
                        "cost_used",
                        "tokens_used",
                        "tasks_completed",
                        "wall_seconds_used",
                        "cost_remaining",
                        "tokens_remaining",
                        "tasks_remaining",
                        "wall_seconds_remaining",
                    )
                }
                if self.budget is not None
                else None
            ),
        }


class EventReducer:
    """Apply one ordered event to an owned mutable accumulator and return it."""

    def apply(self, state: ReplayState, event: Event) -> ReplayState:
        label = f"event {event.sequence_number} ({event.event_type})"
        if not isinstance(event.payload, dict):
            state.diagnostics.append(f"{label}: payload must be an object")
            return state
        kind = event.event_type
        if kind in TASK_EVENTS:
            if not event.task_id:
                state.diagnostics.append(f"{label}: task_id is required")
                return state
            previous = state.task_statuses.get(event.task_id)
            if kind in {"task.completed", "task.failed"} and previous != TaskStatus.RUNNING:
                state.diagnostics.append(f"{label}: {event.task_id} has no active task.started")
            if kind == "task.started":
                if previous not in {None, TaskStatus.PENDING, TaskStatus.READY}:
                    state.diagnostics.append(
                        f"{label}: {event.task_id} cannot start from {previous}"
                    )
                state.task_execution_order.append(event.task_id)
            elif kind == "task.recovered" and previous not in {None, TaskStatus.RUNNING}:
                state.diagnostics.append(f"{label}: recovery requires a running task")
            elif kind == "task.created" and previous is not None:
                state.diagnostics.append(f"{label}: duplicate task creation")
            elif kind == "task.ready" and previous not in {None, TaskStatus.PENDING}:
                state.diagnostics.append(f"{label}: task cannot become ready from {previous}")
            elif kind in {"task.blocked", "task.cancelled"} and previous in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.BLOCKED,
            }:
                state.diagnostics.append(f"{label}: terminal task cannot transition to {kind}")
            state.task_statuses[event.task_id] = TASK_EVENTS[kind]
            if kind == "task.completed" and event.task_id not in state.task_usage:
                raw = event.payload.get("usage")
                try:
                    state.task_usage[event.task_id] = (
                        ResourceUsage(**raw) if raw is not None else None
                    )
                except (TypeError, ValueError) as exc:
                    state.diagnostics.append(f"{label}: invalid usage: {exc}")
        elif kind in RUN_EVENTS:
            if kind in {"run.created", "run.started"} and state.run_status == RunStatus.RUNNING:
                state.diagnostics.append(f"{label}: running run requires run.resumed, not {kind}")
            if kind in {"run.completed", "run.failed"} and state.run_status != RunStatus.RUNNING:
                state.diagnostics.append(f"{label}: terminal run event requires a started run")
            if state.run_status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                state.diagnostics.append(f"{label}: run is already terminal")
            state.run_status = RUN_EVENTS[kind]
        elif kind == "artifact.created":
            artifact_id = event.payload.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                state.diagnostics.append(f"{label}: artifact_id is required")
            elif artifact_id not in state.artifacts_created:
                state.artifacts_created.append(artifact_id)
        elif kind in {"budget.updated", "budget.exhausted"}:
            try:
                state.budget = BudgetSnapshot(**event.payload)
            except (TypeError, ValueError) as exc:
                state.diagnostics.append(f"{label}: invalid budget snapshot: {exc}")
        elif kind == "run.stop_requested":
            reason = event.payload.get("reason")
            if not isinstance(reason, str) or not reason:
                state.diagnostics.append(f"{label}: stop reason is required")
            else:
                state.stop_reason = reason
        elif kind.startswith("research."):
            state.research_events.append(event)
            payload = event.payload
            entity_id = payload.get("entity_id")
            if isinstance(entity_id, str):
                state.research_entities[entity_id] = {**payload, "last_event": kind}
            elif kind == "research.edge.created" and isinstance(payload.get("edge_id"), str):
                state.research_edges[payload["edge_id"]] = dict(payload)
            elif kind == "research.edge.deleted":
                edge_id = payload.get("edge_id")
                if not isinstance(edge_id, str):
                    state.diagnostics.append(f"{label}: edge_id must be a string")
                else:
                    state.research_edges.pop(edge_id, None)
            else:
                state.uninterpreted_events.append(event)
        else:
            state.uninterpreted_events.append(event)
        return state


class ReplayEngine:
    @staticmethod
    def replay(trace: RunTrace, *, strict: bool = True) -> ReplayState:
        check_version(trace.schema_version)
        state = ReplayState()
        state.diagnostics.extend(sequence_errors(trace.events))
        # Sequence validation diagnoses broken streams; permissive replay is useful
        # to inspect them. Never sort by timestamps, even when timestamps go backwards.
        events = sorted(
            trace.events,
            key=lambda event: event.sequence_number if type(event.sequence_number) is int else 0,
        )
        reducer = EventReducer()
        for event in events:
            reducer.apply(state, event)
        if strict:
            ValidationResult(tuple(state.diagnostics)).raise_for_errors()
        return state


def verify_replay(trace: RunTrace) -> ValidationResult:
    """Compare recorded state to event-derived state without filling event gaps."""
    from researchswarm.trace.validation import TraceValidator

    validation = TraceValidator.validate(trace)
    state = ReplayEngine.replay(trace, strict=False)
    errors = list(validation.errors)
    warnings = list(validation.warnings)
    if state.run_status != trace.run.status:
        errors.append(
            f"run status mismatch: stored={trace.run.status}, replayed={state.run_status}"
        )
    for task in trace.tasks:
        status = state.task_statuses.get(task.task_id)
        if status != task.status:
            errors.append(
                f"task {task.task_id} status mismatch: stored={task.status}, replayed={status}"
            )
    if state.usage != trace.total_usage():
        errors.append("usage mismatch between task snapshots and completion events")
    if trace.usage is not None and state.usage != trace.usage:
        errors.append("usage mismatch between declared summary and replay")
    scoped = trace.run.metadata.get("researchswarm", {})
    stop_reason = scoped.get("stop_reason") if isinstance(scoped, dict) else None
    if stop_reason != state.stop_reason:
        errors.append(f"stop reason mismatch: stored={stop_reason}, replayed={state.stop_reason}")
    missing_artifacts = {item.artifact_id for item in trace.artifacts} - set(
        state.artifacts_created
    )
    if missing_artifacts:
        warnings.append(
            "artifacts without creation events (direct storage writes): "
            f"{sorted(missing_artifacts)}"
        )
    return ValidationResult(tuple(dict.fromkeys(errors)), tuple(warnings))
