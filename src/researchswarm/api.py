"""High-level public API."""

from __future__ import annotations

from pathlib import Path

from researchswarm.config import SwarmConfig
from researchswarm.events import EventBus
from researchswarm.exceptions import InvalidTaskError, ResearchSwarmError
from researchswarm.graph import TaskGraph
from researchswarm.models import AgentProtocol, Event, Run, RunStatus, SwarmResult, Task, TaskStatus
from researchswarm.runtime.executor import SwarmExecutor
from researchswarm.runtime.scheduler import SchedulerProtocol
from researchswarm.storage import MemoryStorage, SQLiteStorage, StorageProtocol


class Swarm:
    """Configure and execute a structured swarm run."""

    def __init__(
        self,
        objective: str,
        *,
        storage: str | Path | StorageProtocol | None = None,
        max_concurrency: int = 4,
        fail_fast: bool = False,
        scheduler: SchedulerProtocol | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.run_record = Run(objective=objective, metadata=dict(metadata or {}))
        self.storage = _resolve_storage(storage)
        self.config = SwarmConfig(max_concurrency=max_concurrency, fail_fast=fail_fast)
        self.scheduler = scheduler
        self.events = EventBus()
        self.graph = TaskGraph()
        self._agents: dict[str, AgentProtocol] = {}
        self._executor: SwarmExecutor | None = None
        self._initialized = False
        self._running = False

    @property
    def run_id(self) -> str:
        """The persistent run identifier."""
        return self.run_record.id

    def register_agent(self, agent_type: str, agent: AgentProtocol) -> None:
        """Register any object implementing the async agent protocol."""
        if not agent_type:
            raise ValueError("agent_type must not be empty")
        if not isinstance(agent, AgentProtocol):
            raise TypeError("agent must implement async run(task, context)")
        self._agents[agent_type] = agent

    def add_task(self, task: Task) -> None:
        """Add a task before execution starts."""
        if self._initialized:
            raise InvalidTaskError("cannot add tasks after run initialization")
        self.graph.add_task(task)

    async def run(self) -> SwarmResult:
        """Persist and execute the configured task graph."""
        if self._running:
            raise ResearchSwarmError("this swarm is already running")
        if not self.graph.tasks:
            raise InvalidTaskError("a swarm must contain at least one task")
        self._running = True
        try:
            if not self._initialized:
                await self._initialize()
            self._executor = SwarmExecutor(
                run=self.run_record,
                graph=self.graph,
                storage=self.storage,
                agents=self._agents,
                events=self.events,
                config=self.config,
                scheduler=self.scheduler,
            )
            return await self._executor.execute()
        finally:
            self._running = False

    async def cancel(self) -> None:
        """Cancel active work, or mark a not-yet-started run cancelled."""
        if self._executor is not None and self._running:
            await self._executor.cancel()
            return
        if self.run_record.status is RunStatus.CREATED:
            if not self._initialized:
                await self._initialize()
            self.run_record.transition_to(RunStatus.CANCELLED)
            for task in self.graph.tasks:
                if task.status in {TaskStatus.PENDING, TaskStatus.READY}:
                    task.transition_to(TaskStatus.CANCELLED)
                    await self.storage.save_task(self.run_id, task)
                    await self._emit("task.cancelled", task.id)
            await self.storage.save_run(self.run_record)
            await self._emit("run.cancelled")

    async def close(self) -> None:
        """Close the configured storage backend."""
        await self.storage.close()

    @classmethod
    async def load(
        cls,
        run_id: str,
        *,
        storage: str | Path | StorageProtocol,
        max_concurrency: int = 4,
        fail_fast: bool = False,
        scheduler: SchedulerProtocol | None = None,
    ) -> Swarm:
        """Load an interrupted run and reset RUNNING tasks to PENDING.

        Agents are deliberately not persisted. Register the required agents on
        the returned swarm before calling :meth:`run`.
        """
        backend = _resolve_storage(storage)
        record = await backend.get_run(run_id)
        if record.status not in {RunStatus.CREATED, RunStatus.RUNNING}:
            raise ResearchSwarmError(
                f"run {run_id} is terminal ({record.status.value}) and cannot be resumed"
            )
        swarm = cls(
            record.objective,
            storage=backend,
            max_concurrency=max_concurrency,
            fail_fast=fail_fast,
            scheduler=scheduler,
            metadata=record.metadata,
        )
        swarm.run_record = record
        tasks = await backend.list_tasks(run_id)
        swarm.graph.add_tasks(tasks)
        swarm._initialized = True
        for task in swarm.graph.tasks:
            if task.status is TaskStatus.RUNNING:
                task.transition_to(TaskStatus.PENDING)
                await backend.save_task(run_id, task)
                await swarm._emit(
                    "task.recovered", task.id, {"previous_status": TaskStatus.RUNNING.value}
                )
        return swarm

    async def _initialize(self) -> None:
        self.run_record.root_task_ids = [
            task.id for task in self.graph.tasks if not task.dependencies
        ]
        await self.storage.save_run(self.run_record)
        await self._emit("run.created")
        for task in self.graph.tasks:
            await self.storage.save_task(self.run_id, task)
            await self._emit("task.created", task.id, {"dynamic": False})
        self._initialized = True

    async def _emit(
        self,
        event_type: str,
        task_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> Event:
        event = Event(
            run_id=self.run_id,
            task_id=task_id,
            event_type=event_type,
            payload=dict(payload or {}),
        )
        stored = await self.storage.append_event(event)
        await self.events.publish(stored)
        return stored


def _resolve_storage(storage: str | Path | StorageProtocol | None) -> StorageProtocol:
    if storage is None or storage == "memory":
        return MemoryStorage()
    if isinstance(storage, (str, Path)):
        return SQLiteStorage(storage)
    return storage
