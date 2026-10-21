"""Research semantics stay independent of execution and persist across backends."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from researchswarm import (
    AgentResult,
    Artifact,
    ArtifactNotFoundError,
    Claim,
    ClaimStatus,
    Conflict,
    ConflictStatus,
    Critique,
    CritiqueSeverity,
    Event,
    Evidence,
    Experiment,
    ExperimentResult,
    ExperimentStatus,
    Hypothesis,
    HypothesisStatus,
    MemoryStorage,
    ResearchEdge,
    ResearchGraph,
    ResearchRelation,
    ResearchStorageProtocol,
    ResearchWorkspace,
    Run,
    RunContext,
    RunNotFoundError,
    SerializationError,
    SQLiteStorage,
    Swarm,
    Task,
    TaskNotFoundError,
)
from researchswarm.research.serialization import (
    edge_from_dict,
    edge_to_dict,
    entity_from_dict,
    entity_to_dict,
)


def records():
    h = Hypothesis("X predicts Y", predictions=("association",), confidence=0.5)
    c = Claim("X is associated with Y", status=ClaimStatus.SUPPORTED)
    e = Experiment("Test X", hypothesis_ids=(h.id,), design={"sample": [1, 2]})
    r = ExperimentResult(e.id, "Positive association", metrics={"effect": 0.4, "n": 20})
    v = Evidence("Observed association", "statistics", source_uri="https://example.com/data")
    cr = Critique(r.id, "Small sample", CritiqueSeverity.MAJOR, ("Replicate",))
    co = Conflict(h.id, c.id, "Different scopes", importance=0.2)
    return h, c, e, r, v, cr, co


@pytest.mark.parametrize("entity", records())
def test_entity_round_trip(entity):
    entity.metadata["nested"] = {"values": [1, True, None]}
    data = entity_to_dict(entity)
    assert data["entity_type"]
    restored = entity_from_dict(json.loads(json.dumps(data)))
    assert restored == entity
    assert type(restored) is type(entity)
    assert restored.metadata is not entity.metadata
    assert entity.id.startswith(data["entity_type"] + "_")
    assert entity.created_at.utcoffset() is not None


@pytest.mark.parametrize(
    "cls,kwargs,field",
    [
        (Hypothesis, {"statement": "h"}, "confidence"),
        (Claim, {"statement": "c"}, "confidence"),
        (Evidence, {"description": "v", "evidence_type": "test"}, "strength"),
        (Conflict, {"left_id": "a", "right_id": "b", "description": "d"}, "importance"),
    ],
)
@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True, "high"])
def test_invalid_unit_values(cls, kwargs, field, value):
    with pytest.raises(ValueError):
        cls(**kwargs, **{field: value})


@pytest.mark.parametrize("value", [None, 0, 1, 0.5])
def test_confidence_boundaries(value):
    assert Hypothesis("h", confidence=value).confidence == value
    assert Claim("c", confidence=value).confidence == value


def test_validation_and_defaults():
    assert Hypothesis("h").status is HypothesisStatus.PROPOSED
    assert Claim("c").status is ClaimStatus.OPEN
    assert Experiment("e").status is ExperimentStatus.PROPOSED
    assert Conflict("a", "b", "d").status is ConflictStatus.OPEN
    for kwargs in (
        {"statement": " "},
        {"statement": "h", "id": ""},
        {"statement": "h", "status": "bogus"},
        {"statement": "h", "created_at": datetime(2020, 1, 1)},
    ):
        with pytest.raises(ValueError):
            Hypothesis(**kwargs)
    with pytest.raises(ValueError):
        Conflict("same", "same", "d")
    with pytest.raises(ValueError):
        ExperimentResult("e", "summary", metrics={"nested": {"x": 1}})
    with pytest.raises(SerializationError):
        entity_to_dict(Hypothesis("h", metadata={"bad": object()}))
    with pytest.raises(SerializationError):
        entity_from_dict({"entity_type": "unknown"})
    with pytest.raises(SerializationError):
        edge_from_dict({})
    a, b = Hypothesis("a"), Hypothesis("b")
    a.metadata["x"] = 1
    assert not b.metadata


@pytest.mark.parametrize("relation", [*ResearchRelation, "replicates"])
def test_edges_round_trip(relation):
    edge = ResearchEdge("a", "b", relation, metadata={"note": [1]})
    assert edge_from_dict(json.loads(json.dumps(edge_to_dict(edge)))) == edge
    with pytest.raises(ValueError):
        ResearchEdge("a", "b", "")


def test_graph_queries_and_cycles():
    h, c, e, r, v, cr, co = records()
    # Batch loading accepts references regardless of timestamp / input ordering.
    graph = ResearchGraph([co, cr, r, e, c, h, v])
    edges = [
        ResearchEdge(e.id, h.id, ResearchRelation.TESTS),
        ResearchEdge(r.id, v.id, ResearchRelation.PRODUCES),
        ResearchEdge(v.id, c.id, ResearchRelation.SUPPORTS),
        ResearchEdge(v.id, h.id, ResearchRelation.CONTRADICTS),
        ResearchEdge(c.id, h.id, ResearchRelation.SUPPORTS),
        ResearchEdge(cr.id, r.id, ResearchRelation.CRITIQUES),
        ResearchEdge(h.id, c.id, ResearchRelation.REFINES),
    ]
    for edge in edges:
        graph.add_edge(edge)
    assert graph.evidence_for(c.id) == [v]
    assert graph.evidence_against(h.id) == [v]
    assert graph.evidence_for(h.id) == []  # no transitive support inference
    assert graph.experiments_for(h.id) == [e]  # deduplicates declared and linked
    assert graph.claims_for(h.id) == [c]
    assert graph.critiques_for(r.id) == [cr]
    assert graph.conflicts_for(h.id) == [co]
    assert graph.conflicts_for(c.id) == [co]
    assert graph.entities(Hypothesis) == [h]
    assert {x.id for x in graph.parents(h.id)} == {e.id, v.id, c.id}
    assert graph.children(h.id) == [c]
    assert {x.id for x in graph.related(h.id)} == {e.id, v.id, c.id}
    assert graph.incoming(r.id) == [edges[5]]
    assert graph.outgoing(r.id) == [edges[1]]
    graph.remove_edge(edges[0].id)
    assert graph.experiments_for(h.id) == [e]  # declared reference remains
    assert graph.get(h.id).confidence == 0.5
    snapshot = graph.get(h.id)
    snapshot.metadata["mutated"] = True
    assert not graph.get(h.id).metadata
    with pytest.raises(ValueError):
        graph.add(h)
    with pytest.raises(ValueError):
        graph.add_edge(edges[1])
    with pytest.raises(KeyError):
        graph.add_edge(ResearchEdge(h.id, "missing", "supports"))
    assert len(graph.edges()) == 6


@pytest.mark.parametrize(
    "entity",
    [
        Experiment("e", hypothesis_ids=("missing",)),
        ExperimentResult("missing", "s"),
        Critique("missing", "s"),
        Conflict("missing", "other", "d"),
    ],
)
def test_invalid_embedded_references(entity):
    with pytest.raises(KeyError):
        ResearchGraph().add(entity)


def test_wrong_reference_type_and_missing_queries():
    c = Claim("c")
    graph = ResearchGraph([c])
    with pytest.raises(ValueError):
        graph.add(Experiment("e", hypothesis_ids=(c.id,)))
    with pytest.raises(ValueError):
        graph.add(ExperimentResult(c.id, "s"))
    for query in [
        graph.get,
        graph.parents,
        graph.children,
        graph.incoming,
        graph.outgoing,
        graph.related,
        graph.evidence_for,
        graph.evidence_against,
        graph.experiments_for,
        graph.claims_for,
        graph.critiques_for,
        graph.conflicts_for,
        graph.remove_edge,
    ]:
        with pytest.raises(KeyError):
            query("missing")


@pytest.fixture(params=["memory", "sqlite"])
async def storage(request, tmp_path):
    backend = MemoryStorage() if request.param == "memory" else SQLiteStorage(tmp_path / "test.db")
    yield backend
    await backend.close()


async def test_storage_round_trips_and_isolation(storage):
    assert isinstance(storage, ResearchStorageProtocol)
    run, other = Run("research"), Run("other")
    await storage.save_run(run)
    await storage.save_run(other)
    entities = records()
    for entity in entities:
        await storage.save_research_entity(run.id, entity)
    assert await storage.list_research_entities(run.id) == list(entities)
    assert await storage.list_research_entities(run.id, Hypothesis) == [entities[0]]
    h = entities[0]
    with pytest.raises(ValueError):
        await storage.save_research_entity(run.id, h, create_only=True)
    with pytest.raises(ValueError):
        await storage.save_research_entity(run.id, Claim("wrong type", id=h.id))
    changed = replace(h, status=HypothesisStatus.ACTIVE)
    await storage.save_research_entity(run.id, changed)
    assert await storage.get_research_entity(run.id, h.id) == changed
    with pytest.raises(KeyError):
        await storage.get_research_entity(other.id, h.id)
    with pytest.raises(KeyError):
        await storage.save_research_entity(other.id, Experiment("e", hypothesis_ids=(h.id,)))
    edge = ResearchEdge(h.id, entities[1].id, ResearchRelation.SUPPORTS)
    await storage.save_research_edge(run.id, edge)
    assert await storage.list_research_edges(run.id) == [edge]
    with pytest.raises(KeyError):
        await storage.save_research_edge(other.id, edge)
    with pytest.raises(ValueError):
        await storage.save_research_edge(run.id, edge, create_only=True)
    changed_edge = replace(edge, relation="custom")
    await storage.save_research_edge(run.id, changed_edge)
    assert await storage.list_research_edges(run.id) == [changed_edge]
    loaded = await storage.get_research_entity(run.id, h.id)
    loaded.metadata["mutated"] = []
    assert not (await storage.get_research_entity(run.id, h.id)).metadata
    await storage.delete_research_edge(run.id, edge.id)
    assert await storage.list_research_edges(run.id) == []
    with pytest.raises(KeyError):
        await storage.delete_research_edge(run.id, edge.id)
    with pytest.raises(RunNotFoundError):
        await storage.list_research_entities("missing")
    with pytest.raises(RunNotFoundError):
        await storage.save_research_entity("missing", h)


async def test_runtime_reference_validation(storage):
    run, other = Run("r"), Run("o")
    await storage.save_run(run)
    await storage.save_run(other)
    task = Task("test", "")
    artifact = Artifact("data", "json", data={"n": 10})
    await storage.save_task(other.id, task)
    await storage.save_artifact(other.id, artifact)
    with pytest.raises(TaskNotFoundError):
        await storage.save_research_entity(run.id, Experiment("e", task_ids=(task.id,)))
    with pytest.raises(ArtifactNotFoundError):
        await storage.save_research_entity(run.id, Evidence("v", "data", artifact_id=artifact.id))
    e = Experiment("e", task_ids=(task.id,))
    await storage.save_research_entity(other.id, e)
    await storage.save_research_entity(other.id, Evidence("v", "data", artifact_id=artifact.id))
    with pytest.raises(ArtifactNotFoundError):
        await storage.save_research_entity(
            other.id, ExperimentResult(e.id, "s", artifact_ids=("bad",))
        )
    result = ExperimentResult(e.id, "s", artifact_ids=(artifact.id,))
    await storage.save_research_entity(other.id, result)
    assert await storage.get_research_entity(other.id, result.id) == result


async def test_workspace_and_events(storage):
    run = Run("r")
    await storage.save_run(run)

    async def emit(name, payload):
        return await storage.append_event(Event(run.id, name, payload=payload))

    ws = ResearchWorkspace(run.id, storage, emit)
    h, c, e, r, v, cr, co = records()
    for method, entity in zip(
        [
            ws.add_hypothesis,
            ws.add_claim,
            ws.add_experiment,
            ws.add_result,
            ws.add_evidence,
            ws.add_critique,
            ws.add_conflict,
        ],
        [h, c, e, r, v, cr, co],
        strict=True,
    ):
        assert await method(entity) == entity
    edge = await ws.link(e.id, h, ResearchRelation.TESTS)
    assert (await ws.graph()).experiments_for(h.id) == [e]
    assert await ws.hypotheses() == [h]
    assert await ws.claims() == [c]
    assert await ws.experiments() == [e]
    h.status = HypothesisStatus.SUPPORTED
    await ws.update(h)
    await ws.resolve_conflict(co.id)
    assert (await ws.get(co.id)).status == ConflictStatus.RESOLVED
    assert (await ws.get(h.id)).confidence == 0.5
    events = await storage.list_events(run.id)
    assert [event.event_type for event in events] == [
        "research.hypothesis.created",
        "research.claim.created",
        "research.experiment.created",
        "research.experiment.result_created",
        "research.evidence.created",
        "research.critique.created",
        "research.conflict.created",
        "research.edge.created",
        "research.hypothesis.updated",
        "research.conflict.resolved",
    ]
    assert [event.sequence_number for event in events] == list(range(1, 11))
    assert events[3].payload["experiment_id"] == e.id
    assert events[8].payload["status"] == "supported"
    assert "statement" not in events[0].payload
    with pytest.raises(ValueError):
        await ws.add_hypothesis(h)
    with pytest.raises(KeyError):
        await ws.update(Hypothesis("missing"))
    assert len(await storage.list_events(run.id)) == 10
    await ws.remove_edge(edge.id)
    assert (await ws.graph()).edges() == []


async def test_concurrent_workspace_duplicate_creation(storage):
    run = Run("r")
    await storage.save_run(run)
    workspaces = [ResearchWorkspace(run.id, storage), ResearchWorkspace(run.id, storage)]
    h = Hypothesis("h")
    outcomes = await asyncio.gather(
        *(ws.add_hypothesis(h) for ws in workspaces), return_exceptions=True
    )
    assert sum(isinstance(item, ValueError) for item in outcomes) == 1
    assert await workspaces[0].hypotheses() == [h]


async def test_context_integration_with_executor(storage):
    class Agent:
        async def run(self, task, context):
            ws = ResearchWorkspace.from_context(context)
            await ws.add_hypothesis(Hypothesis("h"))
            return AgentResult(output={"done": True})

    swarm = Swarm("r", storage=storage)
    swarm.register_agent("research", Agent())
    task = Task("research", "", agent_type="research")
    swarm.add_task(task)
    result = await swarm.run()
    assert result.completed_count == 1
    assert len(await storage.list_tasks(result.run_id)) == 1
    events = await storage.list_events(result.run_id)
    event = next(e for e in events if e.event_type == "research.hypothesis.created")
    assert event.task_id == task.id
    assert len(await ResearchWorkspace(result.run_id, storage).hypotheses()) == 1


def test_context_rejects_nonresearch_backend_only_on_opt_in():
    context = RunContext("run", "task", object(), None)
    with pytest.raises(TypeError, match="research persistence"):
        ResearchWorkspace.from_context(context)


async def test_sqlite_reopen_and_phase1_migration(tmp_path):
    path = tmp_path / "old.db"
    # Actual Phase 1 schema, frozen before adding research tables.
    with sqlite3.connect(path) as connection:
        connection.executescript((Path(__file__).parent / "fixtures/phase1_schema.sql").read_text())
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old",
                "old objective",
                "created",
                "2025-01-01T00:00:00+00:00",
                None,
                None,
                "{}",
                "[]",
            ),
        )
    storage = SQLiteStorage(path)
    assert (await storage.get_run("old")).objective == "old objective"
    task = Task("old task", "")
    await storage.save_task("old", task)
    await storage.append_event(Event("old", "legacy.event"))
    ws = ResearchWorkspace("old", storage)
    h = await ws.add_hypothesis(Hypothesis("h"))
    e = await ws.add_experiment(Experiment("e", hypothesis_ids=(h.id,), task_ids=(task.id,)))
    await ws.link(e.id, h.id, ResearchRelation.TESTS)
    await storage.close()
    reopened = SQLiteStorage(path)
    try:
        assert await reopened.get_task("old", task.id) == task
        assert (await reopened.list_events("old"))[0].sequence_number == 1
        graph = await ResearchWorkspace("old", reopened).graph()
        assert graph.experiments_for(h.id) == [e]
        assert len(graph.edges()) == 1
        assert await reopened.list_artifacts("old") == []
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"predictions": "one prediction"},
        {"falsification_criteria": "one criterion"},
        {"rationale": 1},
    ],
)
def test_hypothesis_rejects_invalid_field_types(kwargs):
    with pytest.raises(ValueError):
        Hypothesis("h", **kwargs)


async def test_failed_write_leaves_backend_usable(storage):
    run = Run("r")
    await storage.save_run(run)
    h = Hypothesis("h")
    await storage.save_research_entity(run.id, h)
    with pytest.raises(KeyError):
        await storage.save_research_edge(run.id, ResearchEdge(h.id, "bad", "supports"))
    with pytest.raises(SerializationError):
        await storage.save_research_entity(run.id, replace(h, metadata={"bad": object()}))
    assert await storage.get_research_entity(run.id, h.id) == h
    assert await storage.list_research_edges(run.id) == []
    c = Claim("c")
    await storage.save_research_entity(run.id, c)
    edge = ResearchEdge(h.id, c.id, "supports")
    await storage.save_research_edge(run.id, edge)
    assert await storage.list_research_edges(run.id) == [edge]


async def test_event_failure_does_not_undo_research_write(storage):
    run = Run("r")
    await storage.save_run(run)

    async def failing_emit(name, payload):
        raise RuntimeError("emitter unavailable")

    ws = ResearchWorkspace(run.id, storage, failing_emit)
    h = Hypothesis("h")
    with pytest.raises(RuntimeError, match="emitter unavailable"):
        await ws.add_hypothesis(h)
    assert await ws.get(h.id) == h


async def test_all_entities_and_edges_survive_sqlite_reopen(tmp_path):
    path = tmp_path / "research.db"
    storage = SQLiteStorage(path)
    run = Run("r")
    await storage.save_run(run)
    entities = records()
    for entity in entities:
        await storage.save_research_entity(run.id, entity)
    edges = [
        ResearchEdge(entities[0].id, entities[1].id, relation)
        for relation in [*ResearchRelation, "custom_relation"]
    ]
    for edge in edges:
        await storage.save_research_edge(run.id, edge)
    await storage.close()
    reopened = SQLiteStorage(path)
    try:
        assert await reopened.list_research_entities(run.id) == list(entities)
        assert await reopened.list_research_edges(run.id) == edges
        # SQLite itself enforces run-scoped endpoints, even outside the Python API.
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO research_edges VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "invalid",
                        run.id,
                        entities[0].id,
                        "missing",
                        "supports",
                        entities[0].created_at.isoformat(),
                        "{}",
                    ),
                )
    finally:
        await reopened.close()


async def test_backend_order_is_chronological_across_timezones(storage):
    run = Run("r")
    await storage.save_run(run)
    earlier = Hypothesis("earlier", created_at=datetime.fromisoformat("2025-01-01T10:00:00+02:00"))
    later = Hypothesis("later", created_at=datetime.fromisoformat("2025-01-01T09:00:00+00:00"))
    await storage.save_research_entity(run.id, later)
    await storage.save_research_entity(run.id, earlier)
    assert await storage.list_research_entities(run.id) == [earlier, later]
    edges = [
        ResearchEdge(earlier.id, later.id, "supports", created_at=item.created_at)
        for item in (earlier, later)
    ]
    for edge in reversed(edges):
        await storage.save_research_edge(run.id, edge)
    assert await storage.list_research_edges(run.id) == edges
