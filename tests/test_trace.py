import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researchswarm import (
    AgentResult,
    Artifact,
    BudgetLimits,
    Claim,
    Conflict,
    Critique,
    Event,
    Evidence,
    Experiment,
    ExperimentResult,
    Hypothesis,
    InformationValueScheduler,
    MemoryStorage,
    ReplayEngine,
    ResearchRelation,
    ResearchWorkspace,
    ResourceUsage,
    RunStatus,
    RunTrace,
    SchedulingSignals,
    SQLiteStorage,
    Swarm,
    Task,
    TaskStatus,
    TraceExportOptions,
    TraceValidationError,
    TraceValidator,
    UnsupportedTraceVersionError,
    compare_traces,
    fingerprint_json,
    set_scheduling_signals,
    verify_replay,
)
from researchswarm.trace.codec import event_dict


class ResearchAgent:
    async def run(self, task, context):
        workspace = ResearchWorkspace.from_context(context)
        if task.name == "measure":
            h = await workspace.add_hypothesis(Hypothesis("X predicts Y"))
            experiment = await workspace.add_experiment(
                Experiment("Test X", hypothesis_ids=(h.id,), task_ids=(task.id,))
            )
            artifact = Artifact(
                "sample", "json", data={"secret_dataset": [1, 2]}, producer_task_id=task.id
            )
            await context.storage.save_artifact(context.run_id, artifact)
            result = await workspace.add_result(
                ExperimentResult(experiment.id, "Association", artifact_ids=(artifact.id,))
            )
            evidence = await workspace.add_evidence(
                Evidence("Measured association", "data", artifact_id=artifact.id)
            )
            claim = await workspace.add_claim(Claim("Association in sample"))
            critique = await workspace.add_critique(Critique(result.id, "Replicate"))
            conflict = await workspace.add_conflict(Conflict(h.id, claim.id, "Scope differs"))
            await workspace.resolve_conflict(conflict.id)
            for source, target, relation in [
                (experiment, h, ResearchRelation.TESTS),
                (result, evidence, ResearchRelation.PRODUCES),
                (evidence, claim, ResearchRelation.SUPPORTS),
                (claim, h, ResearchRelation.SUPPORTS),
                (critique, result, ResearchRelation.CRITIQUES),
            ]:
                await workspace.link(source, target, relation)
            temporary = await workspace.link(h, claim, ResearchRelation.REFINES)
            await workspace.remove_edge(temporary.id)
            await context.emit_event("custom.note", {"text": "kept"})
            return AgentResult(
                output={"private_answer": 42},
                artifacts=[artifact],
                usage=ResourceUsage(0.1, 10, 5, 0.2, {"custom": 1}),
            )
        return AgentResult(output={"done": True}, usage=ResourceUsage(0.2, 20, 10, 0.3))


@pytest.fixture(params=["memory", "sqlite"])
async def backend(request, tmp_path):
    storage = MemoryStorage() if request.param == "memory" else SQLiteStorage(tmp_path / "run.db")
    yield storage
    await storage.close()


async def complete(storage):
    swarm = Swarm("trace example", storage=storage)
    swarm.register_agent("research", ResearchAgent())
    first = Task("measure", "Measure", agent_type="research", inputs={"private_prompt": "test"})
    second = Task("analyze", "Analyze", agent_type="research", dependencies=(first.id,))
    swarm.add_task(first)
    swarm.add_task(second)
    return swarm, await swarm.run()


async def test_complete_export_replay_manifest_and_helpers(backend):
    swarm, result = await complete(backend)
    trace = await result.trace()
    validation = TraceValidator.validate(trace)
    assert validation.valid, validation.errors
    verification = verify_replay(trace)
    assert verification.valid, verification.errors
    state = ReplayEngine.replay(trace)
    assert state.run_status is RunStatus.COMPLETED
    assert set(state.task_statuses.values()) == {TaskStatus.COMPLETED}
    assert tuple(state.task_execution_order) == trace.execution_order()
    assert trace.execution_order() == tuple(task.id for task in swarm.graph.tasks)
    assert state.usage == trace.usage
    assert state.usage.total_cost == 0.3
    assert state.usage.input_tokens == 30
    assert state.usage.output_tokens == 15
    assert state.usage.compute_seconds == 0.5
    assert state.usage.tasks_with_usage == 2
    assert state.usage.custom == {"custom": 1}
    assert state.artifacts_created == [trace.artifacts[0].artifact_id]
    assert len(state.research_entities) == 7
    assert len(state.research_edges) == 5
    assert any(event.event_type == "custom.note" for event in state.uninterpreted_events)
    assert any(item.get("status") == "resolved" for item in state.research_entities.values())
    assert len(trace.research_entities_by_type()) == 7
    assert len(trace.tasks_by_agent()["research"]) == 2
    assert len(trace.task_dependencies()[swarm.graph.tasks[1].id]) == 1
    assert trace.duration() >= 0
    assert trace.failed_tasks() == ()
    manifest = trace.manifest()
    assert manifest["run_id"] == result.run_id
    assert manifest["task_count"] == 2
    assert manifest["research_entity_count"] == 7
    assert manifest["total_usage"]["total_cost"] == 0.3
    assert len(manifest["trace_fingerprint"]) == 64
    assert manifest["schema_version"] == "researchswarm.trace.v1"


async def test_json_directory_round_trip_and_exclusion(backend, tmp_path):
    _, result = await complete(backend)
    trace = await result.export_trace(tmp_path / "trace.json", TraceExportOptions(False, False))
    assert RunTrace.load(tmp_path / "trace.json") == trace
    assert all(task.inputs is None and task.output is None for task in trace.tasks)
    assert all(not item.data_included and item.data is None for item in trace.artifacts)
    encoded = (tmp_path / "trace.json").read_text()
    assert "private_prompt" not in encoded
    assert "private_answer" not in encoded
    assert "secret_dataset" not in encoded
    assert trace.fingerprints()["task_inputs"] == {}
    assert trace.fingerprints()["artifact_data"] == {}
    target = tmp_path / "directory"
    trace.to_directory(target)
    assert RunTrace.load(target) == trace
    assert list((target / "artifacts").iterdir()) == []
    lines = (target / "events.jsonl").read_text().splitlines()
    assert len(lines) == len(trace.events)
    assert [json.loads(line) for line in lines] == [event_dict(event) for event in trace.events]
    assert [json.loads(line)["sequence_number"] for line in lines] == list(range(1, len(lines) + 1))
    assert json.loads((target / "manifest.json").read_text()) == trace.manifest()
    with pytest.raises(FileExistsError):
        trace.to_directory(target)
    included = await result.trace(TraceExportOptions(include_artifact_data=True))
    assert included.artifacts[0].data == {"secret_dataset": [1, 2]}
    included.to_directory(tmp_path / "included")
    artifact_files = list((tmp_path / "included/artifacts").iterdir())
    assert len(artifact_files) == 1
    assert (
        json.loads(artifact_files[0].read_text())["artifact_id"]
        == included.artifacts[0].artifact_id
    )
    assert verify_replay(trace).valid


async def test_ordering_and_fingerprints(backend, tmp_path):
    _, result = await complete(backend)
    trace = await result.trace()
    again = await result.trace()
    assert trace.fingerprint() == again.fingerprint()
    assert fingerprint_json({"b": [1, 2], "a": "é"}) == fingerprint_json({"a": "é", "b": [1, 2]})
    assert fingerprint_json({"x": 1}) != fingerprint_json({"x": 2})
    with pytest.raises(ValueError):
        fingerprint_json({"x": float("nan")})
    with pytest.raises(ValueError):
        fingerprint_json({1: "coercion"})
    shuffled = replace(
        trace,
        tasks=tuple(reversed(trace.tasks)),
        artifacts=tuple(reversed(trace.artifacts)),
        research_entities=tuple(reversed(trace.research_entities)),
        research_edges=tuple(reversed(trace.research_edges)),
    )
    assert shuffled.fingerprint() == trace.fingerprint()
    trace.to_json(tmp_path / "a.json")
    shuffled.to_json(tmp_path / "b.json")
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
    # Timestamps do not determine event order.
    events = tuple(
        replace(event, timestamp=datetime(2000, 1, 1, tzinfo=UTC) - timedelta(seconds=i))
        for i, event in enumerate(trace.events)
    )
    backward_clock = replace(trace, events=events)
    assert TraceValidator.validate(backward_clock).valid
    assert (
        ReplayEngine.replay(backward_clock).task_execution_order
        == ReplayEngine.replay(trace).task_execution_order
    )
    assert compare_traces(trace, again)["cost_difference"] == 0
    assert compare_traces(trace, again)["task_count_difference"] == 0


async def test_failed_and_blocked_replay(backend):
    class Agent:
        async def run(self, task, context):
            raise RuntimeError("expected failure")

    swarm = Swarm("failure", storage=backend)
    swarm.register_agent("bad", Agent())
    first = Task("first", "", agent_type="bad")
    second = Task("second", "", agent_type="bad", dependencies=(first.id,))
    swarm.add_task(first)
    swarm.add_task(second)
    result = await swarm.run()
    trace = await result.trace()
    assert verify_replay(trace).valid, verify_replay(trace).errors
    state = ReplayEngine.replay(trace)
    assert state.run_status is RunStatus.FAILED
    assert state.task_statuses == {first.id: TaskStatus.FAILED, second.id: TaskStatus.BLOCKED}
    assert trace.failed_tasks()[0].task_id == first.id
    assert trace.total_usage().tasks_with_usage == 0


async def test_cancelled_replay(backend):
    begun = asyncio.Event()

    class Agent:
        async def run(self, task, context):
            begun.set()
            await asyncio.Event().wait()

    swarm = Swarm("cancel", storage=backend, max_concurrency=1)
    swarm.register_agent("wait", Agent())
    for i in range(2):
        swarm.add_task(Task(str(i), "", agent_type="wait"))
    running = asyncio.create_task(swarm.run())
    await begun.wait()
    await swarm.cancel()
    result = await running
    trace = await result.trace()
    assert verify_replay(trace).valid, verify_replay(trace).errors
    assert ReplayEngine.replay(trace).run_status is RunStatus.CANCELLED
    assert set(ReplayEngine.replay(trace).task_statuses.values()) == {TaskStatus.CANCELLED}


async def test_created_and_running_partial_trace(backend):
    begun, finish = asyncio.Event(), asyncio.Event()

    class Agent:
        async def run(self, task, context):
            begun.set()
            await finish.wait()
            return AgentResult()

    swarm = Swarm("partial", storage=backend)
    swarm.register_agent("wait", Agent())
    task = Task("wait", "", agent_type="wait")
    swarm.add_task(task)
    await swarm._initialize()
    created = await RunTrace.from_storage(swarm.run_id, backend)
    assert verify_replay(created).valid
    assert ReplayEngine.replay(created).run_status is RunStatus.CREATED
    assert created.duration() is None
    running = asyncio.create_task(swarm.run())
    await begun.wait()
    try:
        partial = await RunTrace.from_storage(swarm.run_id, backend)
        assert TraceValidator.validate(partial).valid
        assert verify_replay(partial).valid
        state = ReplayEngine.replay(partial)
        assert state.run_status is RunStatus.RUNNING
        assert state.task_statuses[task.id] is TaskStatus.RUNNING
        assert partial.duration() is None
    finally:
        finish.set()
        await running


async def test_budget_stop_replay(backend):
    class Agent:
        async def run(self, task, context):
            return AgentResult(usage=ResourceUsage(cost=0.2))

    swarm = Swarm(
        "budget",
        storage=backend,
        budget=BudgetLimits(max_cost=0.2),
        scheduler=InformationValueScheduler(),
        max_concurrency=1,
    )
    swarm.register_agent("worker", Agent())
    for i in range(2):
        task = Task(str(i), "", agent_type="worker")
        set_scheduling_signals(task, SchedulingSignals(estimated_cost=0.2))
        swarm.add_task(task)
    trace = await (await swarm.run()).trace()
    assert verify_replay(trace).valid, verify_replay(trace).errors
    state = ReplayEngine.replay(trace)
    assert state.run_status is RunStatus.COMPLETED
    assert state.stop_reason == "budget_exhausted"
    assert state.budget.cost_used == 0.2
    assert state.budget.cost_remaining == 0
    assert state.usage.total_cost == 0.2
    assert TaskStatus.READY in state.task_statuses.values()


async def test_validation_errors_and_future_versions(tmp_path):
    _, result = await complete(MemoryStorage())
    trace = await result.trace()
    data = trace.to_dict()
    data["schema_version"] = "researchswarm.trace.v99"
    with pytest.raises(UnsupportedTraceVersionError):
        RunTrace.from_dict(data)
    (tmp_path / "future.json").write_text(json.dumps(data))
    with pytest.raises(UnsupportedTraceVersionError):
        RunTrace.load(tmp_path / "future.json")
    first = trace.events[0]
    cases = [
        replace(
            trace, events=(first, replace(trace.events[1], sequence_number=1), *trace.events[2:])
        ),
        replace(trace, events=trace.events[1:]),
        replace(trace, events=tuple(reversed(trace.events))),
        replace(trace, events=(replace(first, sequence_number=None), *trace.events[1:])),
        replace(trace, tasks=(*trace.tasks, trace.tasks[0])),
        replace(
            trace, tasks=(replace(trace.tasks[0], dependencies=("missing",)), *trace.tasks[1:])
        ),
        replace(trace, artifacts=(replace(trace.artifacts[0], producer_task_id="missing"),)),
        replace(trace, research_edges=(replace(trace.research_edges[0], target_id="missing"),)),
        replace(trace, events=(replace(first, run_id="other"), *trace.events[1:])),
        replace(trace, events=(replace(first, task_id="missing"), *trace.events[1:])),
        replace(trace, tasks=(replace(trace.tasks[0], status="unknown"), *trace.tasks[1:])),
        replace(trace, exported_at=datetime(2025, 1, 1)),
    ]
    for invalid in cases:
        validation = TraceValidator.validate(invalid)
        assert not validation.valid, invalid
        assert validation.errors
    for field, value in [
        ("status", "unknown"),
        ("created_at", "not a date"),
        ("dependencies", "not an array"),
    ]:
        malformed = trace.to_dict()
        malformed["tasks"][0][field] = value
        with pytest.raises(TraceValidationError):
            RunTrace.from_dict(malformed)


async def test_lifecycle_and_replay_mismatch_diagnostics():
    _, result = await complete(MemoryStorage())
    trace = await result.trace()
    # Keep the sequence intact but remove the meaning of a start event.
    events = tuple(
        replace(event, event_type="custom.not_a_start")
        if event.event_type == "task.started"
        else event
        for event in trace.events
    )
    invalid = replace(trace, events=events)
    assert any("task.started" in error for error in TraceValidator.validate(invalid).errors)
    with pytest.raises(TraceValidationError):
        ReplayEngine.replay(invalid)
    mismatch = replace(
        trace, tasks=(replace(trace.tasks[0], status=TaskStatus.FAILED), *trace.tasks[1:])
    )
    assert any("status mismatch" in error for error in verify_replay(mismatch).errors)
    no_usage = tuple(
        replace(event, payload={}) if event.event_type == "task.completed" else event
        for event in trace.events
    )
    assert any(
        "usage mismatch" in error for error in verify_replay(replace(trace, events=no_usage)).errors
    )


async def test_snapshot_event_gap_is_reported_not_invented():
    storage = MemoryStorage()
    swarm = Swarm("gap", storage=storage)
    task = Task("t", "", agent_type="test")
    swarm.add_task(task)
    await swarm._initialize()
    swarm.run_record.transition_to(RunStatus.RUNNING)
    await storage.save_run(swarm.run_record)
    await storage.append_event(Event(swarm.run_id, "run.started"))
    task.transition_to(TaskStatus.READY)
    task.transition_to(TaskStatus.RUNNING)
    await storage.append_event(Event(swarm.run_id, "task.started", task.id))
    task.transition_to(TaskStatus.COMPLETED)
    task.metadata["researchswarm"] = {"usage": {"cost": 0.2}}
    await storage.save_task(swarm.run_id, task)
    trace = await RunTrace.from_storage(swarm.run_id, storage)
    assert trace.total_usage().total_cost == 0.2
    state = ReplayEngine.replay(trace)
    assert state.usage.total_cost == 0
    assert state.task_statuses[task.id] == TaskStatus.RUNNING
    assert not verify_replay(trace).valid


async def test_cli_export_validate_replay_readonly(tmp_path):
    path = tmp_path / "run.db"
    storage = SQLiteStorage(path)
    _, result = await complete(storage)
    await storage.close()
    before = path.read_bytes()
    output = tmp_path / "trace.json"
    command = [sys.executable, "-m", "researchswarm", "trace"]
    exported = subprocess.run(
        [*command, "export", "--db", str(path), "--run-id", result.run_id, "--output", str(output)],
        capture_output=True,
        text=True,
    )
    assert exported.returncode == 0, exported.stderr
    assert json.loads(exported.stdout)["task_count"] == 2
    assert path.read_bytes() == before
    for action in ("validate", "replay"):
        checked = subprocess.run([*command, action, str(output)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
    missing = subprocess.run(
        [
            *command,
            "export",
            "--db",
            str(tmp_path / "missing.db"),
            "--run-id",
            "bad",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2
    assert not (tmp_path / "missing.db").exists()
    data = json.loads(output.read_text())
    data["events"] = data["events"][1:]
    output.write_text(json.dumps(data))
    invalid = subprocess.run([*command, "validate", str(output)], capture_output=True, text=True)
    assert invalid.returncode == 1
    assert not json.loads(invalid.stdout)["valid"]


async def test_loader_is_independent_of_original_database(tmp_path):
    path = tmp_path / "disposable.db"
    storage = SQLiteStorage(path)
    _, result = await complete(storage)
    await result.export_trace(tmp_path / "portable.json")
    await storage.close()
    path.unlink()
    loaded = RunTrace.load(tmp_path / "portable.json")
    assert verify_replay(loaded).valid
    assert ReplayEngine.replay(loaded).run_status is RunStatus.COMPLETED


def test_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version": "x", "schema_version": "y"}')
    with pytest.raises(TraceValidationError, match="duplicate JSON key"):
        RunTrace.load(path)


async def test_sqlite_recovery_replays_multiple_attempts_once_for_usage(tmp_path):
    class Agent:
        async def run(self, task, context):
            return AgentResult(usage=ResourceUsage(cost=0.1))

    path = tmp_path / "resume.db"
    original = Swarm("resume trace", storage=path)
    task = Task("retry", "", agent_type="worker")
    original.add_task(task)
    await original._initialize()
    original.run_record.transition_to(RunStatus.RUNNING)
    await original.storage.save_run(original.run_record)
    await original.storage.append_event(Event(original.run_id, "run.started"))
    task.transition_to(TaskStatus.READY)
    await original.storage.append_event(Event(original.run_id, "task.ready", task.id))
    task.transition_to(TaskStatus.RUNNING)
    await original.storage.save_task(original.run_id, task)
    await original.storage.append_event(Event(original.run_id, "task.started", task.id))
    await original.close()
    resumed = await Swarm.load(original.run_id, storage=path)
    resumed.register_agent("worker", Agent())
    try:
        trace = await (await resumed.run()).trace()
        assert verify_replay(trace).valid, verify_replay(trace).errors
        state = ReplayEngine.replay(trace)
        assert state.task_execution_order == [task.id, task.id]
        assert state.usage.total_cost == 0.1
        assert state.usage.tasks_with_usage == 1
    finally:
        await resumed.close()


async def test_conflicting_usage_snapshot_is_not_hidden_by_events():
    storage = MemoryStorage()
    _, result = await complete(storage)
    task = (await result.tasks())[0]
    task.metadata["researchswarm"]["usage"]["cost"] = 9
    await storage.save_task(result.run_id, task)
    trace = await result.trace()
    assert trace.total_usage().total_cost >= 9
    assert ReplayEngine.replay(trace).usage.total_cost == 0.3
    assert any("usage mismatch" in error for error in verify_replay(trace).errors)


async def test_generic_storage_contract_without_research():
    class GenericStorage:
        def __init__(self):
            self.backend = MemoryStorage()

        def __getattr__(self, name):
            if "research" in name:
                raise AttributeError(name)
            return getattr(self.backend, name)

    class Agent:
        async def run(self, task, context):
            return AgentResult()

    swarm = Swarm("generic", storage=GenericStorage())
    swarm.register_agent("agent", Agent())
    swarm.add_task(Task("task", "", agent_type="agent"))
    trace = await (await swarm.run()).trace()
    assert trace.research_entities == ()
    assert trace.research_edges == ()
    assert verify_replay(trace).valid


async def test_research_reference_and_payload_validation():
    _, result = await complete(MemoryStorage())
    trace = await result.trace()
    experiment = next(item for item in trace.research_entities if item.entity_type == "experiment")
    invalids = [
        replace(experiment, payload={**experiment.payload, "status": "invalid"}),
        replace(experiment, payload={**experiment.payload, "hypothesis_ids": ["missing"]}),
        replace(experiment, payload={**experiment.payload, "task_ids": ["missing"]}),
        replace(experiment, payload={**experiment.payload, "hypothesis_ids": None}),
        replace(experiment, entity_type="future_type"),
    ]
    for item in invalids:
        entities = tuple(
            item if entity.entity_id == item.entity_id else entity
            for entity in trace.research_entities
        )
        assert not TraceValidator.validate(replace(trace, research_entities=entities)).valid


async def test_exported_paths_do_not_use_artifact_ids(tmp_path):
    _, result = await complete(MemoryStorage())
    trace = await result.trace(TraceExportOptions(include_artifact_data=True))
    dangerous = replace(trace.artifacts[0], artifact_id="../../outside")
    trace = replace(trace, artifacts=(dangerous,))
    trace.to_directory(tmp_path / "safe")
    files = list((tmp_path / "safe/artifacts").iterdir())
    assert len(files) == 1
    assert files[0].name == fingerprint_json("../../outside") + ".json"
    assert not (tmp_path / "outside").exists()


def test_cli_legacy_database_is_not_upgraded(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    fixture = Path(__file__).parent / "fixtures/phase1_schema.sql"
    with sqlite3.connect(path) as db:
        db.executescript(fixture.read_text())
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "legacy", "created", "2025-01-01T00:00:00+00:00", None, None, "{}", "[]"),
        )
    before = path.read_bytes()
    command = [
        sys.executable,
        "-m",
        "researchswarm",
        "trace",
        "export",
        "--db",
        str(path),
        "--run-id",
        "legacy",
        "--output",
        str(tmp_path / "legacy.json"),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert path.read_bytes() == before
    trace = RunTrace.load(tmp_path / "legacy.json")
    assert trace.run.status is RunStatus.CREATED
    assert trace.research_entities == ()
    assert TraceValidator.validate(trace).valid
    assert TraceValidator.validate(trace).warnings
    command[-1] = str(path)
    refused = subprocess.run(command, capture_output=True, text=True)
    assert refused.returncode == 2
    assert path.read_bytes() == before
