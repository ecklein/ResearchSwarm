"""Async DAG executor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from researchswarm.config import SwarmConfig
from researchswarm.events import EventBus
from researchswarm.exceptions import AgentNotFoundError, InvalidTaskError, ResearchSwarmError
from researchswarm.graph import TaskGraph
from researchswarm.models import (
    AgentProtocol,
    AgentResult,
    Event,
    Run,
    RunStatus,
    SwarmResult,
    Task,
    TaskStatus,
)
from researchswarm.runtime.context import RunContext
from researchswarm.runtime.scheduler import PriorityScheduler, SchedulerProtocol
from researchswarm.serialization import ensure_json
from researchswarm.storage.base import StorageProtocol

logger = logging.getLogger(__name__)


class SwarmExecutor:
    """Execute a task DAG with bounded asynchronous concurrency."""

    def __init__(
        self,
        *,
        run: Run,
        graph: TaskGraph,
        storage: StorageProtocol,
        agents: Mapping[str, AgentProtocol],
        events: EventBus,
        config: SwarmConfig,
        scheduler: SchedulerProtocol | None = None,
    ) -> None:
        self.run = run
        self.graph = graph
        self.storage = storage
        self.agents = agents
        self.events = events
        self.config = config
        self.scheduler = scheduler or PriorityScheduler()
        self._cancel_requested = False
        self._active: set[asyncio.Task[None]] = set()

    async def execute(self) -> SwarmResult:
        """Run tasks until the graph succeeds, fails, or is cancelled."""
        self._validate_agents(self.graph.tasks)
        if self.run.status is RunStatus.CREATED:
            self.run.transition_to(RunStatus.RUNNING)
            await self.storage.save_run(self.run)
            await self.emit("run.started")
        elif self.run.status is RunStatus.RUNNING:
            await self.emit("run.resumed")
        else:
            raise ResearchSwarmError(
                f"cannot execute run {self.run.id} in {self.run.status.value} state"
            )
        logger.info("Run started: %s", self.run.id)

        while True:
            await self._refresh_states()
            if self._cancel_requested:
                await self._cancel_remaining()
                break

            ready = self.graph.ready_tasks()
            if not ready:
                break
            selected = self.scheduler.choose(ready, self.config.max_concurrency)
            if not selected:
                break
            await self._run_batch(selected)
            if self.config.fail_fast and any(
                task.status is TaskStatus.FAILED for task in selected
            ):
                self._cancel_requested = True

        await self._refresh_states()
        if self._cancel_requested:
            await self._finish_run(RunStatus.CANCELLED, "run.cancelled")
        elif self.graph.is_complete():
            await self._finish_run(RunStatus.COMPLETED, "run.completed")
        else:
            await self._finish_run(RunStatus.FAILED, "run.failed")
        return await self._result()

    async def cancel(self) -> None:
        """Request cancellation and cancel active agent coroutines."""
        self._cancel_requested = True
        for active in tuple(self._active):
            active.cancel()

    async def emit(
        self,
        event_type: str,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        """Persist and then publish a structured event."""
        event = Event(
            run_id=self.run.id,
            event_type=event_type,
            task_id=task_id,
            payload=ensure_json(payload or {}),
        )
        stored = await self.storage.append_event(event)
        await self.events.publish(stored)
        return stored

    async def _refresh_states(self) -> None:
        changed = True
        while changed:
            changed = False
            for task in self.graph.tasks:
                if task.status is not TaskStatus.PENDING:
                    continue
                dependencies = self.graph.dependencies(task.id)
                failed = [
                    item
                    for item in dependencies
                    if item.status
                    in {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
                ]
                if failed:
                    task.transition_to(TaskStatus.BLOCKED)
                    task.error = f"blocked by dependency {failed[0].id} ({failed[0].status.value})"
                    await self.storage.save_task(self.run.id, task)
                    await self.emit(
                        "task.blocked", task.id, {"dependency_id": failed[0].id}
                    )
                    logger.info("Task blocked: %s", task.id)
                    changed = True
                elif all(item.status is TaskStatus.COMPLETED for item in dependencies):
                    task.transition_to(TaskStatus.READY)
                    await self.storage.save_task(self.run.id, task)
                    await self.emit("task.ready", task.id)
                    changed = True

    async def _run_batch(self, tasks: list[Task]) -> None:
        async with asyncio.TaskGroup() as group:
            for task in tasks:
                active = group.create_task(
                    self._execute_task(task), name=f"researchswarm:{task.id}"
                )
                self._active.add(active)
                active.add_done_callback(self._active.discard)

    async def _execute_task(self, task: Task) -> None:
        try:
            task.transition_to(TaskStatus.RUNNING)
            await self.storage.save_task(self.run.id, task)
            await self.emit("task.started", task.id, {"agent_type": task.agent_type})
            logger.info("Task started: %s", task.id)
            agent = self._agent_for(task)
            context = RunContext(self.run.id, task.id, self.storage, self.emit)
            result = await agent.run(task, context)
            if not isinstance(result, AgentResult):
                raise InvalidTaskError(
                    f"agent for task {task.id} returned {type(result).__name__}, "
                    "expected AgentResult"
                )
            ensure_json(result.output)
            ensure_json(result.metadata)
            await self._integrate_new_tasks(result.new_tasks)
            for artifact in result.artifacts:
                ensure_json(artifact.data)
                ensure_json(artifact.metadata)
                artifact.producer_task_id = task.id
                await self.storage.save_artifact(self.run.id, artifact)
                await self.emit(
                    "artifact.created",
                    task.id,
                    {"artifact_id": artifact.id, "artifact_type": artifact.artifact_type},
                )
            task.output = result.output
            if result.metadata:
                task.metadata = {**task.metadata, "agent_result": result.metadata}
            task.transition_to(TaskStatus.COMPLETED)
            await self.storage.save_task(self.run.id, task)
            await self.emit("task.completed", task.id)
            logger.info("Task completed: %s", task.id)
        except asyncio.CancelledError:
            if task.status in {TaskStatus.READY, TaskStatus.RUNNING}:
                task.transition_to(TaskStatus.CANCELLED)
                task.error = "task cancelled"
                await self.storage.save_task(self.run.id, task)
                await self.emit("task.cancelled", task.id)
            # Cancellation is an expected executor outcome; TaskGroup must keep draining.
        except Exception as exc:
            if task.status is TaskStatus.RUNNING:
                task.transition_to(TaskStatus.FAILED)
                task.error = f"{type(exc).__name__}: {exc}"
                await self.storage.save_task(self.run.id, task)
                await self.emit("task.failed", task.id, {"error": task.error})
                logger.exception("Task failed: %s", task.id)

    async def _integrate_new_tasks(self, tasks: list[Task]) -> None:
        if not tasks:
            return
        if any(task.status is not TaskStatus.PENDING for task in tasks):
            raise InvalidTaskError("dynamically created tasks must have PENDING status")
        self._validate_agents(tasks)
        for task in tasks:
            ensure_json(task.inputs)
            ensure_json(task.metadata)
        self.graph.add_tasks(tasks)
        for task in tasks:
            await self.storage.save_task(self.run.id, task)
            await self.emit("task.created", task.id, {"dynamic": True})

    def _validate_agents(self, tasks: tuple[Task, ...] | list[Task]) -> None:
        for task in tasks:
            if task.agent_type is None or task.agent_type not in self.agents:
                requested = task.agent_type or "<none>"
                raise AgentNotFoundError(
                    f"no agent registered as {requested!r} for task {task.id}"
                )

    def _agent_for(self, task: Task) -> AgentProtocol:
        assert task.agent_type is not None
        try:
            return self.agents[task.agent_type]
        except KeyError as exc:
            raise AgentNotFoundError(f"agent not registered: {task.agent_type}") from exc

    async def _cancel_remaining(self) -> None:
        for task in self.graph.tasks:
            if task.status in {TaskStatus.PENDING, TaskStatus.READY}:
                task.transition_to(TaskStatus.CANCELLED)
                task.error = "run cancelled"
                await self.storage.save_task(self.run.id, task)
                await self.emit("task.cancelled", task.id)

    async def _finish_run(self, status: RunStatus, event_type: str) -> None:
        self.run.transition_to(status)
        await self.storage.save_run(self.run)
        await self.emit(event_type)
        logger.info("Run %s: %s", status.value, self.run.id)

    async def _result(self) -> SwarmResult:
        tasks = list(self.graph.tasks)
        artifacts = await self.storage.list_artifacts(self.run.id)
        if self.run.started_at is None or self.run.completed_at is None:
            raise ResearchSwarmError("finished run is missing timestamps")
        return SwarmResult(
            run_id=self.run.id,
            status=self.run.status,
            task_count=len(tasks),
            completed_count=sum(task.status is TaskStatus.COMPLETED for task in tasks),
            failed_count=sum(task.status is TaskStatus.FAILED for task in tasks),
            blocked_count=sum(task.status is TaskStatus.BLOCKED for task in tasks),
            artifact_ids=[artifact.id for artifact in artifacts],
            started_at=self.run.started_at,
            completed_at=self.run.completed_at,
            _storage=self.storage,
        )
