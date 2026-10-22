import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from researchswarm import (
    AgentResult,
    AnyStopCondition,
    BudgetAwareScheduler,
    BudgetExhaustedStop,
    BudgetLedger,
    BudgetLimits,
    BudgetSnapshot,
    Event,
    InformationValueScheduler,
    MaxTasksStop,
    MemoryStorage,
    NoAffordableTasksStop,
    OrchestrationState,
    PriorityScheduler,
    ResourceUsage,
    RunStatus,
    SchedulingSignals,
    SQLiteStorage,
    StopDecision,
    Swarm,
    Task,
    TaskStatus,
    get_scheduling_signals,
    set_scheduling_signals,
)
from researchswarm.serialization import agent_result_from_dict, task_from_dict, to_dict


def ready(name, *, cost=None, value=1.0, **kwargs):
    task = Task(name, "", agent_type="worker", status=TaskStatus.READY, **kwargs)
    if cost is not None:
        set_scheduling_signals(task, SchedulingSignals(value, value, value, value, cost))
    return task


@pytest.mark.parametrize(
    "name", ["expected_information_gain", "uncertainty", "novelty", "dependency_value"]
)
@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True, "high"])
def test_signals_validate_unit_interval(name, value):
    with pytest.raises(ValueError):
        SchedulingSignals(**{name: value})


@pytest.mark.parametrize(
    "cls,name",
    [
        (SchedulingSignals, "estimated_cost"),
        (SchedulingSignals, "estimated_seconds"),
        (ResourceUsage, "cost"),
        (ResourceUsage, "compute_seconds"),
        (BudgetLimits, "max_cost"),
        (BudgetLimits, "max_wall_seconds"),
    ],
)
@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_resources_are_finite_nonnegative(cls, name, value):
    with pytest.raises(ValueError):
        cls(**{name: value})


@pytest.mark.parametrize(
    "cls,name",
    [
        (SchedulingSignals, "estimated_tokens"),
        (ResourceUsage, "input_tokens"),
        (ResourceUsage, "output_tokens"),
        (BudgetLimits, "max_tokens"),
        (BudgetLimits, "max_tasks"),
    ],
)
@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_resource_counts_are_integers(cls, name, value):
    with pytest.raises(ValueError):
        cls(**{name: value})


def test_metadata_helpers_preserve_user_data_and_round_trip():
    task = Task("t", "", metadata={"user": {"notes": [1]}, "researchswarm": {"other": 2}})
    signals = SchedulingSignals(0.9, 0.8, 0.9, 0.7, 0.2, 30, 2)
    set_scheduling_signals(task, signals, hypothesis_id="hypothesis_1")
    loaded = task_from_dict(json.loads(json.dumps(to_dict(task))))
    assert get_scheduling_signals(loaded) == signals
    assert loaded.metadata["user"] == {"notes": [1]}
    assert loaded.metadata["researchswarm"]["other"] == 2
    assert loaded.metadata["researchswarm"]["hypothesis_id"] == "hypothesis_1"
    set_scheduling_signals(task, SchedulingSignals(estimated_cost=0))
    assert get_scheduling_signals(task).estimated_cost == 0
    assert get_scheduling_signals(Task("missing", "")) is None
    collision = Task("t", "", metadata={"researchswarm": "user value"})
    assert get_scheduling_signals(collision) is None
    with pytest.raises(ValueError):
        set_scheduling_signals(collision, signals)
    assert collision.metadata == {"researchswarm": "user value"}
    loaded.metadata["researchswarm"]["scheduling"] = {"unknown": 1}
    with pytest.raises(ValueError):
        get_scheduling_signals(loaded)


def test_acceptance_score_zero_cost_and_alpha():
    task = ready("a")
    set_scheduling_signals(task, SchedulingSignals(0.9, 0.8, 0.9, 0.7, 0.2))
    policy = InformationValueScheduler()
    assert policy.score(task).score == pytest.approx(2.268)
    assert InformationValueScheduler(alpha=0).score(task).score == pytest.approx(0.4536)
    assert json.loads(json.dumps(asdict(policy.score(task))))["estimated_cost"] == 0.2
    free = ready("free", cost=0)
    assert policy.score(free).score == pytest.approx(1e9)
    with pytest.raises(ValueError):
        InformationValueScheduler(epsilon=0)
    with pytest.raises(ValueError):
        InformationValueScheduler(alpha=-1)
    low = ready("b", cost=0.8, value=0.3)
    assert policy.choose([low, task], 1) == [task]


def test_tie_breaking_and_missing_fallback():
    at = datetime(2025, 1, 1, tzinfo=UTC)
    a = ready("a", cost=1, id="a", priority=0, created_at=at)
    b = ready("b", cost=1, id="b", priority=0, created_at=at)
    older = ready("older", cost=1, id="c", created_at=at - timedelta(seconds=1))
    priority = ready("priority", cost=1, id="d", priority=1, created_at=at)
    tasks = [b, a, priority, older]
    scheduler = InformationValueScheduler()
    assert scheduler.choose(tasks, 4) == [priority, older, a, b]
    assert scheduler.choose(tasks, 0) == []
    generic = ready("generic", priority=5)
    assert scheduler.choose([a, generic], 1) == [generic]
    assert scheduler.score(generic).basis == "priority"
    assert InformationValueScheduler(fallback_to_priority=False).score(generic).score == 0.0625
    assert scheduler.choose([Task("pending", "")], 4) == []
    legacy = [ready("one", priority=1), ready("two", priority=2)]
    assert scheduler.choose(legacy, 2) == PriorityScheduler().choose(legacy, 2)


def test_usage_and_agent_result_compatibility():
    assert AgentResult(output={"old": True}).usage is None
    assert agent_result_from_dict({"output": {"old": True}}).usage is None
    usage = ResourceUsage(0.1, 10, 20, 3, {"gpu_seconds": 2})
    result = AgentResult(output={"done": True}, usage=usage)
    assert agent_result_from_dict(json.loads(json.dumps(to_dict(result)))) == result
    with pytest.raises(ValueError):
        ResourceUsage(custom={"bad": -1})
    with pytest.raises(ValueError):
        ResourceUsage(custom={1: 1})


def test_ledger_arithmetic_and_replay_deduplication():
    limits = BudgetLimits(max_cost=0.3, max_tokens=30, max_tasks=3, max_wall_seconds=10)
    ledger = BudgetLedger(limits)
    assert ledger.record("a", ResourceUsage(0.1, 10, 5))
    assert not ledger.record("a", ResourceUsage(100))
    assert ledger.record("b", ResourceUsage(0.2, 10, 5))
    ledger.record("c")
    ledger.observe_wall_seconds(4)
    ledger.observe_wall_seconds(2)
    snapshot = ledger.snapshot()
    assert snapshot == BudgetSnapshot(0.3, 30, 3, 4, 0, 0, 0, 6)
    assert ledger.exhausted()
    assert not ledger.can_afford(SchedulingSignals(estimated_cost=0.1))
    events = [
        Event("r", "task.completed", "a", payload={"usage": asdict(ResourceUsage(0.1))}),
        Event("r", "task.completed", "b", payload={"usage": asdict(ResourceUsage(0.2))}),
    ]
    events += [events[0], Event("r", "task.completed", "c")]
    replay = BudgetLedger.from_events(events, limits)
    assert replay.snapshot().cost_used == 0.3
    assert replay.snapshot().tasks_completed == 3
    assert BudgetLedger().can_afford(SchedulingSignals(estimated_cost=100000))
    assert not BudgetLedger().exhausted()
    with pytest.raises(ValueError):
        BudgetLedger.from_events(events + [Event("other", "task.completed", "d")])
    assert BudgetLedger.from_events(events, run_id="other").snapshot().tasks_completed == 0


def test_budget_batch_reservations_and_skips():
    budget = BudgetLedger(BudgetLimits(max_cost=0.5, max_tokens=10, max_tasks=2))
    a, b, c = ready("a", cost=0.3), ready("b", cost=0.3), ready("c", cost=0.2)
    for task in (a, b, c):
        # Static priority gives an explicit ordering separate from cost/value ranking.
        task.priority = {"a": 3, "b": 2, "c": 1}[task.name]
    policy = BudgetAwareScheduler(PriorityScheduler(), budget)
    assert policy.choose([c, b, a], 3) == [a, c]
    assert b.status is TaskStatus.READY
    assert budget.snapshot().cost_used == 0  # reservations do not count as actual usage
    expensive = ready("expensive", cost=100, priority=100)
    assert policy.choose([expensive, a, c], 2) == [a, c]
    set_scheduling_signals(a, SchedulingSignals(estimated_cost=0.3, estimated_tokens=11))
    assert policy.choose([a, b, c], 2) == [b, c]
    assert BudgetAwareScheduler(InformationValueScheduler()).choose([b, c], 1) == [c]


def test_time_estimates_are_parallel_not_additive():
    tasks = [ready(str(i), cost=0) for i in range(2)]
    for task in tasks:
        set_scheduling_signals(task, SchedulingSignals(estimated_cost=0, estimated_seconds=6))
    ledger = BudgetLedger(BudgetLimits(max_wall_seconds=10))
    policy = BudgetAwareScheduler(ledger=ledger)
    assert policy.choose(tasks, 2) == tasks
    ledger.observe_wall_seconds(5)
    assert policy.choose(tasks, 2) == []


def test_stop_conditions():
    state = OrchestrationState(
        "r", (ready("a", cost=2),), ("done",), BudgetLedger(BudgetLimits(max_cost=1)).snapshot()
    )
    assert not BudgetExhaustedStop().should_stop(state).stop
    assert NoAffordableTasksStop().should_stop(state).reason == "no_affordable_tasks"
    assert MaxTasksStop(1).should_stop(state).reason == "max_tasks"
    combined = AnyStopCondition([MaxTasksStop(1), NoAffordableTasksStop()])
    assert combined.should_stop(state).reason == "max_tasks"
    assert combined.limit_slots(state, 10) == 0
    zero = replace(state, budget=BudgetLedger(BudgetLimits(max_cost=0)).snapshot())
    assert BudgetExhaustedStop().should_stop(zero).reason == "budget_exhausted"
    assert not NoAffordableTasksStop().should_stop(replace(state, ready_tasks=())).stop
    assert not BudgetExhaustedStop().should_stop(replace(state, budget=None)).stop
    with pytest.raises(ValueError):
        StopDecision(True)


class UsageAgent:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.started = []

    async def run(self, task, context):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.started.append(task.name)
        await asyncio.sleep(0.001)
        self.active -= 1
        return AgentResult(
            usage=ResourceUsage(**task.inputs["usage"]) if "usage" in task.inputs else None
        )


@pytest.fixture(params=["memory", "sqlite"])
async def backend(request, tmp_path):
    storage = MemoryStorage() if request.param == "memory" else SQLiteStorage(tmp_path / "run.db")
    yield storage
    await storage.close()


def add_tasks(swarm, specs):
    tasks = []
    for name, estimate, actual, value in specs:
        task = Task(name, "", agent_type="worker", inputs={"usage": {"cost": actual}})
        set_scheduling_signals(task, SchedulingSignals(value, value, value, value, estimate))
        swarm.add_task(task)
        tasks.append(task)
    return tasks


async def test_concurrency_budget_and_explanatory_events(backend):
    swarm = Swarm(
        "budget",
        storage=backend,
        scheduler=InformationValueScheduler(),
        budget=BudgetLimits(max_cost=0.5),
        max_concurrency=2,
        trace_scores=True,
    )
    agent = UsageAgent()
    swarm.register_agent("worker", agent)
    tasks = add_tasks(
        swarm,
        [("A", 0.2, 0.2, 0.9), ("B", 0.8, 0.8, 0.4), ("C", 0.3, 0.3, 0.8), ("D", 0.1, 0.1, 0.1)],
    )
    result = await swarm.run()
    assert result.status is RunStatus.COMPLETED
    assert agent.started == ["A", "C"]
    assert agent.maximum == 2
    assert [task.status for task in tasks] == [
        TaskStatus.COMPLETED,
        TaskStatus.READY,
        TaskStatus.COMPLETED,
        TaskStatus.READY,
    ]
    assert (await backend.get_run(result.run_id)).metadata["researchswarm"][
        "stop_reason"
    ] == "budget_exhausted"
    events = await result.events()
    assert [e.sequence_number for e in events] == list(range(1, len(events) + 1))
    selected = next(e for e in events if e.event_type == "scheduler.tasks_selected")
    assert selected.payload["task_ids"] == [tasks[0].id, tasks[2].id]
    assert selected.payload["policy"] == "InformationValueScheduler"
    assert len(selected.payload["scores"]) == 4
    assert any(e.event_type == "scheduler.task_scored" for e in events)
    ledger = BudgetLedger.from_events(events, swarm.budget)
    assert ledger.snapshot().cost_used == 0.5
    assert ledger.snapshot().tasks_completed == 2
    assert [e.payload["cost_used"] for e in events if e.event_type == "budget.updated"] == [0, 0.5]


@pytest.mark.parametrize(
    "actual,expected_count,reason", [(0.1, 3, None), (0.6, 1, "budget_exhausted")]
)
async def test_estimate_actual_difference(actual, expected_count, reason):
    swarm = Swarm(
        "estimate",
        max_concurrency=1,
        scheduler=InformationValueScheduler(),
        budget=BudgetLimits(max_cost=0.5),
    )
    swarm.register_agent("worker", UsageAgent())
    add_tasks(swarm, [(str(i), 0.3, actual, 1) for i in range(3)])
    result = await swarm.run()
    assert result.completed_count == expected_count
    assert result.status is RunStatus.COMPLETED
    assert swarm.run_record.metadata["researchswarm"].get("stop_reason") == reason


async def test_no_affordable_stop_and_missing_usage(backend):
    swarm = Swarm("small", storage=backend, budget=BudgetLimits(max_cost=0.2))
    swarm.register_agent("worker", UsageAgent())
    add_tasks(swarm, [("expensive", 0.3, 0.3, 1)])
    result = await swarm.run()
    assert result.completed_count == 0
    assert result.status is RunStatus.COMPLETED
    assert swarm.run_record.metadata["researchswarm"]["stop_reason"] == "no_affordable_tasks"
    second = Swarm(
        "unknown usage", storage=backend, budget=BudgetLimits(max_tasks=1), max_concurrency=4
    )
    second.register_agent("worker", UsageAgent())
    for i in range(3):
        second.add_task(Task(str(i), "", agent_type="worker"))
    result = await second.run()
    ledger = BudgetLedger.from_events(await result.events())
    assert result.completed_count == 1
    assert ledger.snapshot().cost_used == 0
    assert ledger.snapshot().tasks_completed == 1


@pytest.mark.parametrize(
    "limits",
    [
        BudgetLimits(max_cost=0),
        BudgetLimits(max_tokens=0),
        BudgetLimits(max_tasks=0),
        BudgetLimits(max_wall_seconds=0),
    ],
)
async def test_zero_budget_admits_no_tasks(limits):
    swarm = Swarm("zero", budget=limits)
    agent = UsageAgent()
    swarm.register_agent("worker", agent)
    add_tasks(swarm, [("even free", 0, 0, 1)])
    result = await swarm.run()
    assert result.status is RunStatus.COMPLETED
    assert result.completed_count == 0
    assert not agent.started


async def test_legacy_and_contextual_scheduler_compatibility():
    class Legacy:
        def choose(self, ready_tasks, available_slots):
            return list(reversed(ready_tasks))[:available_slots]

    class Contextual:
        def __init__(self):
            self.contexts = []

        def choose_with_context(self, ready_tasks, available_slots, context):
            self.contexts.append(context)
            return list(ready_tasks)[:available_slots]

    for scheduler in (Legacy(), Contextual()):
        swarm = Swarm("compat", scheduler=scheduler, max_concurrency=1)
        swarm.register_agent("worker", UsageAgent())
        add_tasks(swarm, [("a", 1, 0, 1), ("b", 1, 0, 1)])
        result = await swarm.run()
        assert result.completed_count == 2
        assert result.status is RunStatus.COMPLETED
        if isinstance(scheduler, Contextual):
            assert scheduler.contexts[0].budget is None
            assert len(scheduler.contexts[1].completed_task_ids) == 1
            assert scheduler.contexts[0].run_id == result.run_id


async def test_max_tasks_stop_caps_concurrent_batch():
    swarm = Swarm("stop", max_concurrency=4, stop_condition=AnyStopCondition([MaxTasksStop(1)]))
    swarm.register_agent("worker", UsageAgent())
    add_tasks(swarm, [(str(i), 1, 0, 1) for i in range(4)])
    result = await swarm.run()
    assert result.completed_count == 1
    assert result.status is RunStatus.COMPLETED
    assert swarm.run_record.metadata["researchswarm"]["stop_reason"] == "max_tasks"


async def test_budget_stop_does_not_hide_agent_error():
    class Agent:
        async def run(self, task, context):
            if task.name == "bad":
                raise RuntimeError("agent failed")
            return AgentResult(usage=ResourceUsage(cost=0.5))

    swarm = Swarm("errors", budget=BudgetLimits(max_cost=0.5), max_concurrency=2)
    swarm.register_agent("worker", Agent())
    add_tasks(swarm, [("bad", 0.2, 0, 1), ("good", 0.2, 0.5, 1), ("left", 0.2, 0, 1)])
    result = await swarm.run()
    assert result.status is RunStatus.FAILED
    assert result.failed_count == 1


@pytest.mark.parametrize("with_event", [True, False])
async def test_sqlite_resume_preserves_usage_without_double_counting(tmp_path, with_event):
    path = tmp_path / "resume.db"
    original = Swarm(
        "resume",
        storage=path,
        budget=BudgetLimits(max_cost=0.5, max_tasks=2),
        scheduler=InformationValueScheduler(),
        max_concurrency=2,
    )
    tasks = add_tasks(
        original, [("done", 0.2, 0.2, 1), ("recover", 0.3, 0.3, 1), ("skip", 0.1, 0.1, 0.1)]
    )
    await original._initialize()
    original.run_record.transition_to(RunStatus.RUNNING)
    await original.storage.save_run(original.run_record)
    first, interrupted, _ = tasks
    for task in (first, interrupted):
        task.transition_to(TaskStatus.READY)
        task.transition_to(TaskStatus.RUNNING)
    first.transition_to(TaskStatus.COMPLETED)
    first.metadata["researchswarm"]["usage"] = asdict(ResourceUsage(cost=0.2))
    await original.storage.save_task(original.run_id, first)
    await original.storage.save_task(original.run_id, interrupted)
    if with_event:
        for _ in range(2):  # duplicate delivery/replay cannot double-charge a task
            await original.storage.append_event(
                Event(
                    original.run_id,
                    "task.completed",
                    first.id,
                    payload={"usage": asdict(ResourceUsage(cost=0.2))},
                )
            )
    await original.close()
    loaded = await Swarm.load(original.run_id, storage=path, scheduler=InformationValueScheduler())
    agent = UsageAgent()
    loaded.register_agent("worker", agent)
    assert loaded.budget == BudgetLimits(max_cost=0.5, max_tasks=2)
    result = await loaded.run()
    assert agent.started == ["recover"]
    assert result.completed_count == 2
    ledger = BudgetLedger.from_events(
        await result.events(), loaded.budget, tasks=await result.tasks()
    )
    assert ledger.snapshot().cost_used == 0.5
    assert ledger.snapshot().tasks_completed == 2
    assert result.status is RunStatus.COMPLETED
    await loaded.close()


async def test_wall_budget_counts_downtime_on_resume(tmp_path):
    path = tmp_path / "wall.db"
    original = Swarm("wall", storage=path, budget=BudgetLimits(max_wall_seconds=1))
    add_tasks(original, [("pending", 0, 0, 1)])
    await original._initialize()
    original.run_record.transition_to(
        RunStatus.RUNNING, at=datetime.now(UTC) - timedelta(seconds=10)
    )
    await original.storage.save_run(original.run_record)
    await original.close()
    loaded = await Swarm.load(original.run_id, storage=path)
    loaded.register_agent("worker", UsageAgent())
    result = await loaded.run()
    assert result.completed_count == 0
    assert result.status is RunStatus.COMPLETED
    assert loaded.run_record.metadata["researchswarm"]["stop_reason"] == "budget_exhausted"
    await loaded.close()


async def test_dynamic_research_tasks_use_new_signals(backend):
    from researchswarm import Hypothesis, ResearchWorkspace

    class Agent:
        async def run(self, task, context):
            workspace = ResearchWorkspace.from_context(context)
            if task.name == "design":
                hypothesis = await workspace.add_hypothesis(Hypothesis("X predicts Y"))
                high = Task("high", "", agent_type="worker", dependencies=(task.id,))
                low = Task("low", "", agent_type="worker", dependencies=(task.id,))
                set_scheduling_signals(
                    high, SchedulingSignals(0.9, 0.8, 0.9, 0.7, 0.2), hypothesis_id=hypothesis.id
                )
                set_scheduling_signals(
                    low, SchedulingSignals(0.4, 0.3, 0.3, 0.4, 0.8), hypothesis_id=hypothesis.id
                )
                return AgentResult(new_tasks=[low, high], usage=ResourceUsage(cost=0.1))
            hypothesis_id = task.metadata["researchswarm"]["hypothesis_id"]
            assert (await workspace.get(hypothesis_id)).statement == "X predicts Y"
            return AgentResult(usage=ResourceUsage(cost=0.2))

    swarm = Swarm(
        "dynamic research",
        storage=backend,
        scheduler=InformationValueScheduler(),
        budget=BudgetLimits(max_cost=0.4),
        max_concurrency=2,
    )
    swarm.register_agent("worker", Agent())
    parent = Task("design", "", agent_type="worker")
    set_scheduling_signals(parent, SchedulingSignals(estimated_cost=0.1))
    swarm.add_task(parent)
    result = await swarm.run()
    statuses = {task.name: task.status for task in await result.tasks()}
    assert statuses == {
        "design": TaskStatus.COMPLETED,
        "high": TaskStatus.COMPLETED,
        "low": TaskStatus.READY,
    }
    assert BudgetLedger.from_events(await result.events()).snapshot().cost_used == 0.3
    assert swarm.run_record.metadata["researchswarm"]["stop_reason"] == "no_affordable_tasks"


async def test_concurrent_actual_overrun_is_recorded_and_stops_next_round():
    arrived = asyncio.Event()
    starts = []

    class Agent:
        async def run(self, task, context):
            starts.append(task.id)
            if len(starts) == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), timeout=1)
            return AgentResult(usage=ResourceUsage(cost=0.3, input_tokens=8, output_tokens=2))

    swarm = Swarm("overrun", budget=BudgetLimits(max_cost=0.4, max_tokens=20), max_concurrency=2)
    swarm.register_agent("worker", Agent())
    tasks = add_tasks(swarm, [(str(i), 0.2, 0.3, 1) for i in range(3)])
    for task in tasks:
        set_scheduling_signals(task, SchedulingSignals(estimated_cost=0.2, estimated_tokens=10))
    result = await swarm.run()
    assert result.status is RunStatus.COMPLETED
    assert len(starts) == 2
    ledger = BudgetLedger.from_events(await result.events(), swarm.budget)
    assert ledger.snapshot().cost_used == 0.6
    assert ledger.snapshot().cost_remaining == 0
    assert ledger.snapshot().tokens_used == 20
    assert ledger.snapshot().tasks_completed == 2


async def test_token_reservations_prevent_batch_oversubscription():
    swarm = Swarm("tokens", budget=BudgetLimits(max_tokens=10), max_concurrency=3)
    agent = UsageAgent()
    swarm.register_agent("worker", agent)
    for index, tokens in enumerate([6, 6, 4]):
        task = Task(str(index), "", agent_type="worker", inputs={"usage": {"input_tokens": tokens}})
        set_scheduling_signals(task, SchedulingSignals(estimated_tokens=tokens))
        swarm.add_task(task)
    result = await swarm.run()
    assert agent.started == ["0", "2"]
    assert result.completed_count == 2
    assert BudgetLedger.from_events(await result.events()).snapshot().tokens_used == 10


async def test_sqlite_resume_explicitly_overrides_limits(tmp_path):
    original = Swarm("override", storage=tmp_path / "override.db", budget=BudgetLimits(max_cost=0))
    add_tasks(original, [("a", 1, 1, 1)])
    await original._initialize()
    run_id = original.run_id
    await original.close()
    loaded = await Swarm.load(run_id, storage=tmp_path / "override.db", budget=BudgetLimits())
    loaded.register_agent("worker", UsageAgent())
    result = await loaded.run()
    assert result.completed_count == 1
    assert loaded.run_record.metadata["researchswarm"]["budget_limits"]["max_cost"] is None
    await loaded.close()


async def test_cancellation_during_scheduling_does_not_launch_selected_tasks():
    swarm = Swarm("cancel during scheduling", budget=BudgetLimits(max_cost=10))
    agent = UsageAgent()
    swarm.register_agent("worker", agent)
    add_tasks(swarm, [("a", 1, 1, 1)])

    async def cancel(event):
        await swarm.cancel()

    swarm.events.subscribe("scheduler.round_started", cancel)
    result = await swarm.run()
    assert result.status is RunStatus.CANCELLED
    assert not agent.started


async def test_default_agents_preserve_metadata_without_signals_or_usage():
    swarm = Swarm("legacy metadata")
    swarm.register_agent("worker", UsageAgent())
    original_metadata = {"user": {"x": 1}, "researchswarm": "legacy user value"}
    task = Task("old", "", agent_type="worker", metadata=original_metadata)
    swarm.add_task(task)
    result = await swarm.run()
    assert result.completed_count == 1
    assert (await result.tasks())[0].metadata == original_metadata
    event = next(e for e in await result.events() if e.event_type == "task.completed")
    assert event.payload == {}
