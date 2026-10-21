"""Dependency graph implementation without external graph libraries."""

from __future__ import annotations

from collections.abc import Iterable

from researchswarm.exceptions import CycleError, DuplicateTaskError, TaskNotFoundError
from researchswarm.models import Task, TaskStatus


class TaskGraph:
    """A mutable directed acyclic graph of tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._dependencies: dict[str, set[str]] = {}
        self._dependents: dict[str, set[str]] = {}

    @property
    def tasks(self) -> tuple[Task, ...]:
        """Return tasks in deterministic creation order."""
        return tuple(sorted(self._tasks.values(), key=lambda task: (task.created_at, task.id)))

    def get_task(self, task_id: str) -> Task:
        """Return a task by identifier."""
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise TaskNotFoundError(f"task not found: {task_id}") from exc

    def add_task(self, task: Task) -> None:
        """Add one task, requiring its dependencies to already exist."""
        self.add_tasks([task])

    def add_tasks(self, tasks: Iterable[Task]) -> None:
        """Atomically add tasks, allowing dependencies among the batch."""
        additions = list(tasks)
        ids = [task.id for task in additions]
        if len(ids) != len(set(ids)):
            raise DuplicateTaskError("new task identifiers must be unique")
        duplicate = next((task_id for task_id in ids if task_id in self._tasks), None)
        if duplicate:
            raise DuplicateTaskError(f"task already exists: {duplicate}")
        known = set(self._tasks) | set(ids)
        for task in additions:
            missing = set(task.dependencies) - known
            if missing:
                raise TaskNotFoundError(
                    f"task {task.id} has missing dependencies: {sorted(missing)}"
                )

        for task in additions:
            self._tasks[task.id] = task
            self._dependencies[task.id] = set(task.dependencies)
            self._dependents.setdefault(task.id, set())
            for dependency in task.dependencies:
                self._dependents.setdefault(dependency, set()).add(task.id)
        try:
            self.validate()
        except Exception:
            for task in additions:
                self._tasks.pop(task.id, None)
                self._dependencies.pop(task.id, None)
                self._dependents.pop(task.id, None)
            self._rebuild_dependents()
            raise

    def add_dependency(self, task_id: str, depends_on_task_id: str) -> None:
        """Add one edge, rolling it back if it creates a cycle."""
        task = self.get_task(task_id)
        self.get_task(depends_on_task_id)
        if depends_on_task_id in self._dependencies[task_id]:
            return
        self._dependencies[task_id].add(depends_on_task_id)
        self._dependents[depends_on_task_id].add(task_id)
        try:
            self.validate()
        except CycleError:
            self._dependencies[task_id].remove(depends_on_task_id)
            self._dependents[depends_on_task_id].remove(task_id)
            raise
        task.dependencies = tuple(sorted(self._dependencies[task_id]))

    def dependencies(self, task_id: str) -> tuple[Task, ...]:
        """Return the task's direct dependencies."""
        self.get_task(task_id)
        return tuple(self._tasks[item] for item in sorted(self._dependencies[task_id]))

    def dependents(self, task_id: str) -> tuple[Task, ...]:
        """Return tasks directly depending on the given task."""
        self.get_task(task_id)
        return tuple(self._tasks[item] for item in sorted(self._dependents[task_id]))

    def ready_tasks(self) -> list[Task]:
        """Return READY tasks in deterministic order."""
        return [task for task in self.tasks if task.status is TaskStatus.READY]

    def is_complete(self) -> bool:
        """Return whether every task completed successfully."""
        return bool(self._tasks) and all(
            task.status is TaskStatus.COMPLETED for task in self._tasks.values()
        )

    def validate(self) -> None:
        """Validate references and reject cycles using DFS."""
        for task_id, dependencies in self._dependencies.items():
            missing = dependencies - self._tasks.keys()
            if missing:
                raise TaskNotFoundError(
                    f"task {task_id} has missing dependencies: {sorted(missing)}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str, path: list[str]) -> None:
            if task_id in visiting:
                start = path.index(task_id)
                cycle = path[start:] + [task_id]
                raise CycleError(f"dependency cycle detected: {' -> '.join(cycle)}")
            if task_id in visited:
                return
            visiting.add(task_id)
            path.append(task_id)
            for dependency in sorted(self._dependencies[task_id]):
                visit(dependency, path)
            path.pop()
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in sorted(self._tasks):
            visit(task_id, [])

    def _rebuild_dependents(self) -> None:
        self._dependents = {task_id: set() for task_id in self._tasks}
        for task_id, dependencies in self._dependencies.items():
            for dependency in dependencies:
                self._dependents[dependency].add(task_id)

