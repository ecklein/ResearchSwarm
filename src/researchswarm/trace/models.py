"""Stable interchange DTOs, intentionally separate from runtime persistence models."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from researchswarm.models.run import RunStatus
from researchswarm.models.task import TaskStatus
from researchswarm.models.usage import ResourceUsage, nonnegative


@dataclass(frozen=True)
class TraceExportOptions:
    include_task_inputs: bool = True
    include_task_outputs: bool = True
    include_artifact_data: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.include_task_inputs,
            self.include_task_outputs,
            self.include_artifact_data,
        ):
            if not isinstance(value, bool):
                raise ValueError("trace export options must be booleans")


@dataclass(frozen=True)
class RunTraceRun:
    run_id: str
    objective: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    metadata: dict[str, Any]
    root_task_ids: tuple[str, ...]


@dataclass(frozen=True)
class TaskTrace:
    task_id: str
    name: str
    description: str
    agent_type: str | None
    dependencies: tuple[str, ...]
    status: TaskStatus
    priority: float
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    inputs: dict[str, Any] | None
    output: dict[str, Any] | None
    error: str | None
    metadata: dict[str, Any]
    usage: ResourceUsage | None = None


@dataclass(frozen=True)
class ArtifactTrace:
    artifact_id: str
    name: str
    type: str
    producer_task_id: str | None
    uri: str | None
    metadata: dict[str, Any]
    created_at: datetime
    data_included: bool = False
    data: Any = None


@dataclass(frozen=True)
class ResearchEntityTrace:
    entity_id: str
    entity_type: str
    created_at: datetime
    metadata: dict[str, Any]
    # Type-specific fields only; identity/type/time/metadata live in the envelope.
    payload: dict[str, Any]


@dataclass(frozen=True)
class UsageSummary:
    total_cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    compute_seconds: float = 0.0
    tasks_with_usage: int = 0
    custom: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        nonnegative(self.tasks_with_usage, "tasks_with_usage", integer=True)
        ResourceUsage(
            self.total_cost,
            self.input_tokens,
            self.output_tokens,
            self.compute_seconds,
            self.custom,
        )

    @classmethod
    def aggregate(cls, usages: Iterable[ResourceUsage | None]) -> UsageSummary:
        values = [usage for usage in usages if usage is not None]

        def total(numbers: Iterable[float]) -> float:
            return float(sum((Decimal(str(value)) for value in numbers), Decimal(0)))

        keys = sorted({key for usage in values for key in usage.custom})
        return cls(
            total(usage.cost for usage in values),
            sum(usage.input_tokens for usage in values),
            sum(usage.output_tokens for usage in values),
            total(usage.compute_seconds for usage in values),
            len(values),
            {key: total(usage.custom.get(key, 0) for usage in values) for key in keys},
        )
