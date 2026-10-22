"""Provider-independent resource usage reported explicitly by agents."""

from dataclasses import dataclass, field
from math import isfinite


def nonnegative(value: float | int, name: str, *, integer: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (integer and not isinstance(value, int))
        or not isfinite(value)
        or value < 0
    ):
        raise ValueError(
            f"{name} must be a finite non-negative {'integer' if integer else 'number'}"
        )


@dataclass(frozen=True)
class ResourceUsage:
    """Cost is an application-defined scalar; compute time is not wall time."""

    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    compute_seconds: float = 0.0
    custom: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        nonnegative(self.cost, "cost")
        nonnegative(self.input_tokens, "input_tokens", integer=True)
        nonnegative(self.output_tokens, "output_tokens", integer=True)
        nonnegative(self.compute_seconds, "compute_seconds")
        if not isinstance(self.custom, dict):
            raise ValueError("custom usage must be a dictionary")
        for key, value in self.custom.items():
            if not isinstance(key, str) or not key:
                raise ValueError("custom usage names must be nonempty strings")
            nonnegative(value, key)
