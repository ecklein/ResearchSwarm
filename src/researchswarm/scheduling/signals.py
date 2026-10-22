"""JSON-safe task metadata for externally supplied scheduling estimates."""

from dataclasses import asdict, dataclass
from typing import Any

from researchswarm.models.task import Task
from researchswarm.models.usage import nonnegative
from researchswarm.serialization import ensure_json


@dataclass(frozen=True)
class SchedulingSignals:
    expected_information_gain: float = 0.5
    uncertainty: float = 0.5
    novelty: float = 0.5
    dependency_value: float = 0.5
    estimated_cost: float = 1.0
    estimated_tokens: int | None = None
    estimated_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("expected_information_gain", "uncertainty", "novelty", "dependency_value"):
            value = getattr(self, name)
            nonnegative(value, name)
            if value > 1:
                raise ValueError(f"{name} must be within [0, 1]")
        nonnegative(self.estimated_cost, "estimated_cost")
        if self.estimated_tokens is not None:
            nonnegative(self.estimated_tokens, "estimated_tokens", integer=True)
        if self.estimated_seconds is not None:
            nonnegative(self.estimated_seconds, "estimated_seconds")


def namespace(metadata: dict[str, Any]) -> dict[str, Any]:
    """Copy our namespace without overwriting unrelated user metadata."""
    value = metadata.get("researchswarm", {})
    if not isinstance(value, dict):
        raise ValueError("metadata['researchswarm'] must be a dictionary")
    return dict(value)


def set_scheduling_signals(
    task: Task, signals: SchedulingSignals, *, hypothesis_id: str | None = None
) -> None:
    data = ensure_json(asdict(signals))
    SchedulingSignals(**data)
    scoped = namespace(task.metadata)
    scoped["scheduling"] = data
    if hypothesis_id is not None:
        if not isinstance(hypothesis_id, str) or not hypothesis_id:
            raise ValueError("hypothesis_id must be a nonempty string")
        scoped["hypothesis_id"] = hypothesis_id
    metadata = {**task.metadata, "researchswarm": scoped}
    ensure_json(metadata)
    task.metadata = metadata


def get_scheduling_signals(task: Task) -> SchedulingSignals | None:
    """Missing signals return None; malformed explicit signals raise ValueError."""
    scoped = task.metadata.get("researchswarm")
    if not isinstance(scoped, dict) or "scheduling" not in scoped:
        return None
    data = scoped["scheduling"]
    if not isinstance(data, dict):
        raise ValueError("scheduling signals must be a dictionary")
    try:
        return SchedulingSignals(**ensure_json(data))
    except TypeError as exc:
        raise ValueError(f"invalid scheduling signals: {exc}") from exc
