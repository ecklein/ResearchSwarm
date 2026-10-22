"""Async DAG executor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import asdict
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
from researchswarm.models.usage import ResourceUsage
from researchswarm.runtime.context import RunContext
from researchswarm.runtime.scheduler import PriorityScheduler, SchedulerProtocol
from researchswarm.scheduling.budget import BudgetLedger, BudgetLimits, BudgetSnapshot
from researchswarm.scheduling.context import ContextualSchedulerProtocol, SchedulingContext
from researchswarm.scheduling.policies import (
    BudgetAwareScheduler,
    ExplainableSchedulerProtocol,
    choose_tasks,
)
from researchswarm.scheduling.signals import SchedulingSignals, get_scheduling_signals, namespace
from researchswarm.scheduling.stopping import (
    AnyStopCondition,
    BudgetExhaustedStop,
    NoAffordableTasksStop,
    OrchestrationState,
    SlotLimitProtocol,
    StopConditionProtocol,
)
from researchswarm.serialization import ensure_json
from researchswarm.storage.base import StorageProtocol
from researchswarm.utils import utc_now

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
        scheduler: SchedulerProtocol | ContextualSchedulerProtocol | None = None,
        budget: BudgetLimits | None = None,
        stop_condition: StopConditionProtocol | None = None,
        trace_scores: bool = False,
    ) -> None:
        self.run = run
        self.graph = graph
        self.storage = storage
        self.agents = agents
        self.events = events
        self.config = config
        self.scheduler: SchedulerProtocol | ContextualSchedulerProtocol = (
            scheduler or PriorityScheduler()
        )
        self.budget = budget
        self.ledger = BudgetLedger(budget)
        if budget is not None and not isinstance(self.scheduler, BudgetAwareScheduler):
            self.scheduler = BudgetAwareScheduler(self.scheduler)
        conditions: list[StopConditionProtocol] = []
        if budget is not None:
            conditions.extend([BudgetExhaustedStop(), NoAffordableTasksStop()])
        if stop_condition is not None:
            conditions.append(stop_condition)
        self.stop_condition = AnyStopCondition(conditions)
        self.trace_scores = trace_scores
        self._stop_reason: str | None = None
        self._round = 0
        self._cancel_requested = False
        self._active: set[asyncio.Task[None]] = set()

    async def execute(self) -> SwarmResult:
        """Run tasks until the graph succeeds, fails, or is cancelled."""
        self._validate_agents(self.graph.tasks)
        history = await self.storage.list_events(self.run.id)
        self.ledger = BudgetLedger.from_events(
            history,
            self.budget,
            tasks=self.graph.tasks,
            run_id=self.run.id,
        )
        self._round = sum(event.event_type == "scheduler.round_started" for event in history)
        scoped = self.run.metadata.get("researchswarm", {})
        if isinstance(scoped, dict):
            self._stop_reason = scoped.get("stop_reason")
        if self.budget is not None:
            scoped = namespace(self.run.metadata)
            scoped["budget_limits"] = asdict(self.budget)
            self.run.metadata = {**self.run.metadata, "researchswarm": scoped}
            await self.storage.save_run(self.run)
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
        if self.budget is not None:
            await self.emit("budget.updated", payload=asdict(self._budget_snapshot()))

        while self._stop_reason is None:
            await self._refresh_states()
            if self._cancel_requested:
                await self._cancel_remaining()
                break

            ready = self.graph.ready_tasks()
            if not ready:
                break
            state = OrchestrationState(
                self.run.id,
                tuple(ready),
                tuple(task.id for task in self.graph.tasks if task.status is TaskStatus.COMPLETED),
                self._budget_snapshot() if self.budget is not None else None,
            )
            decision = self.stop_condition.should_stop(state)
            if decision.stop:
                assert decision.reason is not None
                await self._request_stop(decision.reason)
                break
            slots = self.config.max_concurrency
            if isinstance(self.stop_condition, SlotLimitProtocol):
                slots = self.stop_condition.limit_slots(state, slots)
            selected = await self._select_tasks(ready, slots, state)
            if self._cancel_requested:
                await self._cancel_remaining()
                break
            if not selected:
                break
            await self._run_batch(selected)
            if self.budget is not None:
                await self.emit("budget.updated", payload=asdict(self._budget_snapshot()))
            if self.config.fail_fast and any(task.status is TaskStatus.FAILED for task in selected):
                self._cancel_requested = True

        await self._refresh_states()
        if self._cancel_requested:
            await self._finish_run(RunStatus.CANCELLED, "run.cancelled")
        elif (
            self._stop_reason is not None
            and not any(
                task.status in {TaskStatus.FAILED, TaskStatus.BLOCKED} for task in self.graph.tasks
            )
            or self.graph.is_complete()
        ):
            await self._finish_run(RunStatus.COMPLETED, "run.completed")
        else:
            await self._finish_run(RunStatus.FAILED, "run.failed")
        return await self._result()

    def _budget_snapshot(self) -> BudgetSnapshot:
        if self.run.started_at is not None:
            # Wall budget is elapsed time since the original start, including downtime.
            self.ledger.observe_wall_seconds(
                max(0.0, (utc_now() - self.run.started_at).total_seconds())
            )
        return self.ledger.snapshot()

    async def _request_stop(self, reason: str) -> None:
        self._stop_reason = reason
        scoped = namespace(self.run.metadata)
        scoped["stop_reason"] = reason
        self.run.metadata = {**self.run.metadata, "researchswarm": scoped}
        await self.storage.save_run(self.run)
        if reason == "budget_exhausted":
            await self.emit("budget.exhausted", payload=asdict(self._budget_snapshot()))
        await self.emit("run.stop_requested", payload={"reason": reason})

    async def _select_tasks(
        self,
        ready: list[Task],
        slots: int,
        state: OrchestrationState,
    ) -> list[Task]:
        self._round += 1
        context = SchedulingContext(
            self.run.id,
            state.budget,
            state.completed_task_ids,
            {"ready_tasks": len(ready), "completed_tasks": len(state.completed_task_ids)},
        )
        await self.emit(
            "scheduler.round_started",
            payload={
                "round": self._round,
                "available_slots": slots,
                "budget": asdict(state.budget) if state.budget else None,
            },
        )
        selected = choose_tasks(self.scheduler, ready, slots, context)
        scores = (
            self.scheduler.score_tasks(ready)
            if isinstance(self.scheduler, ExplainableSchedulerProtocol)
            else []
        )
        candidates = []
        for task in ready:
            item: dict[str, Any] = {
                "task_id": task.id,
                "priority": task.priority,
                "created_at": task.created_at.isoformat(),
            }
            if self.budget is not None:
                item["estimates"] = asdict(get_scheduling_signals(task) or SchedulingSignals())
            candidates.append(item)
        if self.trace_scores:
            for score in scores:
                await self.emit("scheduler.task_scored", score.task_id, asdict(score))
        policy = self.scheduler
        if isinstance(policy, BudgetAwareScheduler):
            policy = policy.scheduler
        await self.emit(
            "scheduler.tasks_selected",
            payload={
                "round": self._round,
                "policy": type(policy).__name__,
                "budget_aware": isinstance(self.scheduler, BudgetAwareScheduler),
                "task_ids": [task.id for task in selected],
                "candidates": candidates,
                "scores": [asdict(score) for score in scores],
            },
        )
        return selected

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
            usage = None
            if result.usage is not None:
                if not isinstance(result.usage, ResourceUsage):
                    raise InvalidTaskError("AgentResult.usage must be ResourceUsage or None")
                usage = ResourceUsage(**ensure_json(asdict(result.usage)))
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
            if usage is not None:
                scoped = namespace(task.metadata)
                scoped["usage"] = asdict(usage)
                task.metadata = {**task.metadata, "researchswarm": scoped}
            task.transition_to(TaskStatus.COMPLETED)
            await self.storage.save_task(self.run.id, task)
            self.ledger.record(task.id, usage)
            await self.emit(
                "task.completed",
                task.id,
                {"usage": asdict(usage)} if usage is not None else {},
            )
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
