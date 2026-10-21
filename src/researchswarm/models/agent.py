"""Framework-agnostic agent interface and result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from researchswarm.models.artifact import Artifact
from researchswarm.models.task import Task

if TYPE_CHECKING:
    from researchswarm.runtime.context import RunContext


@dataclass
class AgentResult:
    """Structured output returned by an agent."""

    output: dict[str, Any] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    new_tasks: list[Task] = field(default_factory=list)


@runtime_checkable
class AgentProtocol(Protocol):
    """The only interface an agent must implement."""

    async def run(self, task: Task, context: RunContext) -> AgentResult:
        """Execute a task and return a structured result."""
        ...

