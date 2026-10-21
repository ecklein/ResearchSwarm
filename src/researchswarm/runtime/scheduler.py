"""Replaceable task scheduling policy."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from researchswarm.models import Task, TaskStatus


class SchedulerProtocol(Protocol):
    """Protocol for task selection policies."""

    def choose(self, ready_tasks: Sequence[Task], available_slots: int) -> list[Task]:
        """Choose at most available_slots tasks from the ready set."""
        ...


class PriorityScheduler:
    """Choose READY tasks by priority, creation time, then identifier."""

    def choose(self, ready_tasks: Sequence[Task], available_slots: int) -> list[Task]:
        if available_slots <= 0:
            return []
        candidates = (task for task in ready_tasks if task.status is TaskStatus.READY)
        return sorted(candidates, key=lambda task: (-task.priority, task.created_at, task.id))[
            :available_slots
        ]

