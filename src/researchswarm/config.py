"""Runtime configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SwarmConfig:
    """Configuration for task execution."""

    max_concurrency: int = 4
    fail_fast: bool = False

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")

