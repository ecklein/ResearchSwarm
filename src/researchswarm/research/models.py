"""Domain-agnostic research records; all changes are explicit, never inferred."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, TypeAlias

from researchswarm.utils import ensure_aware, generate_id, utc_now


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class ClaimStatus(StrEnum):
    OPEN = "open"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


class ExperimentStatus(StrEnum):
    PROPOSED = "proposed"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CritiqueSeverity(StrEnum):
    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class ConflictStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _unit(value: float | None, name: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1
    ):
        raise ValueError(f"{name} must be within [0, 1]")


def _strings(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be a sequence of strings")
    for value in values:
        _text(value, name)
    return tuple(values)


@dataclass(kw_only=True)
class _Entity:
    created_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_aware(self.created_at, "created_at")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dictionary")


@dataclass
class Hypothesis(_Entity):
    statement: str
    rationale: str = ""
    predictions: tuple[str, ...] = ()
    falsification_criteria: tuple[str, ...] = ()
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    confidence: float | None = None
    id: str = field(default_factory=lambda: generate_id("hypothesis"))

    def __post_init__(self) -> None:
        super().__post_init__()
        _text(self.id, "id")
        _text(self.statement, "statement")
        _unit(self.confidence, "confidence")
        if not isinstance(self.rationale, str):
            raise ValueError("rationale must be a string")
        self.status = HypothesisStatus(self.status)
        self.predictions = _strings(self.predictions, "predictions")
        self.falsification_criteria = _strings(
            self.falsification_criteria, "falsification_criteria"
        )


@dataclass
class Claim(_Entity):
    statement: str
    status: ClaimStatus = ClaimStatus.OPEN
    confidence: float | None = None
    id: str = field(default_factory=lambda: generate_id("claim"))

    def __post_init__(self) -> None:
        super().__post_init__()
        _text(self.id, "id")
        _text(self.statement, "statement")
        _unit(self.confidence, "confidence")
        self.status = ClaimStatus(self.status)


@dataclass
class Evidence(_Entity):
    description: str
    evidence_type: str
    artifact_id: str | None = None
    source_uri: str | None = None
    strength: float | None = None
    id: str = field(default_factory=lambda: generate_id("evidence"))

    def __post_init__(self) -> None:
        super().__post_init__()
        _text(self.id, "id")
        _text(self.description, "description")
        _text(self.evidence_type, "evidence_type")
        _unit(self.strength, "strength")
        for name in ("artifact_id", "source_uri"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)


@dataclass
class Experiment(_Entity):
    name: str
    description: str = ""
    hypothesis_ids: tuple[str, ...] = ()
    design: dict[str, Any] = field(default_factory=dict)
    status: ExperimentStatus = ExperimentStatus.PROPOSED
    task_ids: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: generate_id("experiment"))

    def __post_init__(self) -> None:
        super().__post_init__()
        _text(self.id, "id")
        _text(self.name, "name")
        if not isinstance(self.description, str):
            raise ValueError("description must be a string")
        self.status = ExperimentStatus(self.status)
        self.hypothesis_ids = _strings(self.hypothesis_ids, "hypothesis_ids")
        self.task_ids = _strings(self.task_ids, "task_ids")
        if not isinstance(self.design, dict):
            raise ValueError("design must be a dictionary")


@dataclass
class ExperimentResult(_Entity):
    experiment_id: str
    summary: str
    metrics: dict[str, float | int | str | bool | None] = field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    successful: bool = True
    id: str = field(default_factory=lambda: generate_id("result"))

    def __post_init__(self) -> None:
        super().__post_init__()
        _text(self.id, "id")
        _text(self.experiment_id, "experiment_id")
        _text(self.summary, "summary")
        self.artifact_ids = _strings(self.artifact_ids, "artifact_ids")
        if not isinstance(self.successful, bool):
            raise ValueError("successful must be a bool")
        if not isinstance(self.metrics, dict) or any(
            not isinstance(key, str)
            or (value is not None and not isinstance(value, (float, int, str, bool)))
            for key, value in self.metrics.items()
        ):
            raise ValueError("metrics must map strings to scalar values")
        if any(isinstance(value, float) and not isfinite(value) for value in self.metrics.values()):
            raise ValueError("numeric metrics must be finite")


@dataclass
class Critique(_Entity):
    target_id: str
    summary: str
    severity: CritiqueSeverity = CritiqueSeverity.INFO
    recommendations: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: generate_id("critique"))

    def __post_init__(self) -> None:
        super().__post_init__()
        _text(self.id, "id")
        _text(self.target_id, "target_id")
        _text(self.summary, "summary")
        self.severity = CritiqueSeverity(self.severity)
        self.recommendations = _strings(self.recommendations, "recommendations")


@dataclass
class Conflict(_Entity):
    left_id: str
    right_id: str
    description: str
    importance: float | None = None
    status: ConflictStatus = ConflictStatus.OPEN
    id: str = field(default_factory=lambda: generate_id("conflict"))

    def __post_init__(self) -> None:
        super().__post_init__()
        _text(self.id, "id")
        _text(self.left_id, "left_id")
        _text(self.right_id, "right_id")
        _text(self.description, "description")
        if self.left_id == self.right_id:
            raise ValueError("a conflict requires two different entities")
        _unit(self.importance, "importance")
        self.status = ConflictStatus(self.status)


ResearchEntity: TypeAlias = (
    Hypothesis | Claim | Evidence | Experiment | ExperimentResult | Critique | Conflict
)

ENTITY_TYPES: dict[str, type[ResearchEntity]] = {
    "hypothesis": Hypothesis,
    "claim": Claim,
    "evidence": Evidence,
    "experiment": Experiment,
    "result": ExperimentResult,
    "critique": Critique,
    "conflict": Conflict,
}


def entity_type(entity: ResearchEntity) -> str:
    for name, cls in ENTITY_TYPES.items():
        if type(entity) is cls:
            return name
    raise TypeError(f"unsupported research entity: {type(entity).__name__}")
