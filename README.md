# ResearchSwarm

ResearchSwarm is a lightweight orchestration substrate for multi-agent research systems.

Instead of treating a conversation as the primary unit of state, ResearchSwarm coordinates
framework-agnostic agents through structured tasks, dependencies, artifacts, events, and durable
run state. Phase 2 adds explicit, persistent research semantics above the small execution runtime.

## Why ResearchSwarm?

- Explicit task DAGs make dependencies and failures visible.
- Any object with an async `run(task, context)` method can be an agent.
- Independent work runs concurrently with a configurable bound.
- Structured outputs flow directly to dependent tasks.
- Memory and SQLite backends share one small storage contract.
- Ordered event traces support logging, replay, and future evaluation tooling.
- Runtime code contains no LLM provider or research-domain assumptions.

ResearchSwarm requires Python 3.11 or newer and has no runtime dependencies outside the standard
library.

## Installation

From a checkout:

```bash
python -m pip install -e .
```

For development:

```bash
python -m pip install -e '.[dev]'
pytest
```

## Quickstart

```python
import asyncio

from researchswarm import AgentResult, Swarm, Task


class ResearchAgent:
    async def run(self, task, context):
        await asyncio.sleep(0.1)
        return AgentResult(output={"finding": f"Finished {task.name}"})


class SynthesisAgent:
    async def run(self, task, context):
        findings = await context.get_dependency_results()
        return AgentResult(output={"combined": findings})


async def main():
    swarm = Swarm(
        objective="Demonstrate structured parallel research",
        storage="researchswarm.db",  # omit for in-memory storage
        max_concurrency=3,
    )
    swarm.register_agent("researcher", ResearchAgent())
    swarm.register_agent("synthesizer", SynthesisAgent())

    branches = [
        Task(name=f"Research {topic}", description=f"Investigate {topic}", agent_type="researcher")
        for topic in ("A", "B", "C")
    ]
    synthesis = Task(
        name="Synthesis",
        description="Combine the findings",
        agent_type="synthesizer",
        dependencies=tuple(task.id for task in branches),
    )
    for task in [*branches, synthesis]:
        swarm.add_task(task)

    result = await swarm.run()
    print(result.status, result.completed_count)
    print((await result.tasks())[-1].output)
    await swarm.close()


asyncio.run(main())
```

The three research tasks become ready together and run concurrently. Synthesis becomes ready only
after all three complete, then reads their persisted outputs.

## Agent protocol

Agents do not subclass a ResearchSwarm base class. They satisfy this structural protocol:

```python
class AgentProtocol(Protocol):
    async def run(self, task: Task, context: RunContext) -> AgentResult: ...
```

`AgentResult` can contain a JSON-compatible output dictionary, artifacts, result metadata, and new
tasks. This keeps adapters for OpenAI Agents SDK, LangGraph, local models, raw API calls, or ordinary
deterministic Python workers outside the runtime.

## Task graphs and parallel execution

Each `Task` identifies its dependencies by UUID. The built-in `TaskGraph` rejects missing references
and cycles. The executor persists each transition:

```text
PENDING -> READY -> RUNNING -> COMPLETED
                           `-> FAILED
PENDING/READY -> BLOCKED or CANCELLED
```

A failed task blocks its descendants while unrelated branches continue. `PriorityScheduler` selects
ready tasks by descending priority, then creation time and ID, so selection is deterministic even
though concurrent completion order may vary. `max_concurrency` is always enforced.

## Outputs and artifacts

Task outputs are JSON-compatible dictionaries stored separately from inputs. A dependent agent reads
them with:

```python
upstream = await context.get_dependency_results()
```

Agents can also return generic durable artifacts:

```python
return AgentResult(
    output={"summary": "..."},
    artifacts=[Artifact(name="notes", artifact_type="json", data={"claims": []})],
)
```

SQLite stores artifact metadata and JSON-compatible data. Large binary/blob storage is outside Phase
1; use `uri` to reference externally managed data.

## Dynamic expansion

An agent may return `new_tasks`. ResearchSwarm validates agent registrations, IDs, dependency
references, JSON fields, and acyclicity before adding the batch to the active graph:

```python
return AgentResult(
    new_tasks=[Task(name="Follow-up", description="...", agent_type="researcher")]
)
```

Tasks in one returned batch may depend on peers in that same batch.

## Persistence and resume

Use no `storage` argument (or `"memory"`) for `MemoryStorage`. Pass a filesystem path for built-in
SQLite persistence:

```python
swarm = Swarm("Objective", storage="runs.db")
```

SQLite uses parameterized SQL, creates its schema automatically, and stores runs, tasks, dependency
edges, artifacts, and ordered events. To resume a process-interrupted run:

```python
swarm = await Swarm.load(run_id, storage="runs.db")
swarm.register_agent("researcher", ResearchAgent())
result = await swarm.run()
```

Agent objects are not persisted and must be registered again. On load, tasks left `RUNNING` are reset
to `PENDING` and a `task.recovered` event is emitted. Completed work remains completed. Terminal runs
cannot be resumed.

## Events

Every event is persisted before listeners run and receives a monotonically increasing sequence number
within its run. Subscribe with a synchronous or asynchronous callback:

```python
swarm.events.subscribe("task.completed", lambda event: print(event.task_id))
swarm.events.subscribe("*", trace_listener)
```

Core events include run creation/start/completion/failure, task creation/readiness/start/completion/
failure/blocking/cancellation, artifact creation, and recovery. Listener failures are logged without
breaking execution.

## Research semantics (Phase 2)

**TaskGraph = what should execute and in what order.**

**ResearchGraph = what the swarm currently believes, has tested, and has evidence for or against.**

These are separate graphs. Execution dependencies form a DAG; research relationships can contain
cycles. An experiment is a research record that can reference multiple runtime task IDs. Adding an
experiment does not schedule tasks, execute work, or change any hypothesis's status or confidence.

The research layer provides `Hypothesis`, `Claim`, `Evidence`, `Experiment`, `ExperimentResult`,
`Critique`, and `Conflict` dataclasses, with explicit status/severity enums. `ResearchEdge` connects
records using `ResearchRelation` values such as `TESTS`, `SUPPORTS`, `CONTRADICTS`, and `CRITIQUES`.
Custom nonempty relation strings are also supported. Evidence and results can reference existing
durable artifacts, and all records support JSON-compatible metadata.

```python
from researchswarm import (
    Experiment, Hypothesis, MemoryStorage, ResearchRelation, ResearchWorkspace, Run,
)

async def research_example():
    storage = MemoryStorage()
    run = Run("Study X and Y")
    await storage.save_run(run)  # A workspace belongs to an existing run.
    workspace = ResearchWorkspace(run.id, storage)
    h = await workspace.add_hypothesis(Hypothesis(
        statement="Feature X predicts target Y",
        rationale="A proposed shared mechanism.",
    ))
    e = await workspace.add_experiment(Experiment(
        name="Test X against Y",
        hypothesis_ids=(h.id,),
        design={"sample_size": 100},
    ))
    await workspace.link(e.id, h.id, ResearchRelation.TESTS)
    graph = await workspace.graph()
    assert graph.experiments_for(h.id) == [e]
```

Inside an agent, use `workspace = ResearchWorkspace.from_context(context)`. The context stays
unchanged and ordinary agents continue using only the generic runtime API. Context workspaces emit
research events through the runtime's existing ordered, persisted event stream, associated with the
current task. Standalone workspaces may receive an async `emit(event_type, payload)` callback; with
no emitter they persist research state without events. Payloads contain IDs, types, and statuses,
not large record contents. Persistence precedes emission; the two writes are not one transaction,
and an emitter failure does not undo the research write.

Use `workspace.update(entity)` for explicit changes and `workspace.resolve_conflict(conflict_id)`
for explicit resolution. These emit update/resolution events when an emitter is present. There is
no automatic confidence scoring, conflict detection, hypothesis promotion, or experiment selection.

Graph reads return detached snapshots in creation-time/ID order. `incoming` and `outgoing` return
edges; `parents`, `children`, and `related` return direct neighboring entities, deduplicated across
relations. `evidence_for` and `evidence_against` select direct incoming evidence edges; support is
not inferred transitively. `claims_for` selects supporting or contradicting claims. Experiment and
critique queries include declared `hypothesis_ids`/`target_id` references as well as explicit edges;
conflict queries match either side. Declared references never create implicit edges. Filter with
`graph.entities(Hypothesis)` or use workspace `hypotheses()`, `claims()`, and `experiments()` helpers.

Both built-in backends implement the optional `ResearchStorageProtocol`; the executor still requires
only `StorageProtocol`. Writes validate research, task, and artifact references within the same run.
Referenced records must already be persisted. In particular, an artifact returned from an agent in
`AgentResult` is persisted **after** that agent returns; to reference it during the agent's execution,
persist it first using `context.storage.save_artifact(context.run_id, artifact)`.
Graph/workspace additions reject duplicate IDs, and missing research IDs raise `KeyError`.
Storage `save_research_entity`/`save_research_edge` methods upsert by default; `create_only=True`
rejects duplicates atomically. Entity types cannot change during updates. Writes use last-writer-wins
semantics; workspace graph reads are detached views, not multi-call transactional snapshots.

SQLite adds indexed `research_entities` and `research_edges` tables with JSON payloads and run-scoped
foreign keys. Existing Phase 1 databases remain loadable. No runtime dependencies were added.

See [`examples/research_graph.py`](examples/research_graph.py) for a deterministic example that
persists entities, artifacts, edges, and events, reopens SQLite, and prints a research summary next
to the separate execution plan.

## Architecture

```text
Swarm public API
      |
      v
SwarmExecutor -> SchedulerProtocol
      |                 |
      v                 v
  TaskGraph       PriorityScheduler
      |
      +----> AgentProtocol + RunContext
      |
      +----> StorageProtocol -> MemoryStorage / SQLiteStorage
      |
      `----> persisted Event -> EventBus listeners
```

The replaceable extension points are `AgentProtocol`, `SchedulerProtocol`, `StorageProtocol`, and
event listeners. The executor depends only on these abstractions and generic task semantics.

## Examples

- [`examples/simple_parallel.py`](examples/simple_parallel.py): three concurrent branches followed by synthesis.
- [`examples/dependency_graph.py`](examples/dependency_graph.py): a sequential pipeline consuming dependency outputs.
- [`examples/dynamic_expansion.py`](examples/dynamic_expansion.py): an agent expands the active DAG.
- [`examples/custom_agent.py`](examples/custom_agent.py): a minimal deterministic custom agent.

Run any example from the repository root after installing the package.

## Non-goals and roadmap

Phase 2 provides structured epistemic state, not an automated scientist. Semantic deduplication,
automatic conflict detection, confidence updates, experiment selection, research scheduling,
embeddings, LLM calls, provider wrappers, and SwarmBench evaluation remain outside this phase.
Dynamic research orchestration and evaluation can build on the relationship:

```text
Research semantics -> ResearchSwarm runtime -> Structured event trace -> SwarmBench
```

## License

MIT
