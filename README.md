# ResearchSwarm

ResearchSwarm is a lightweight orchestration substrate for multi-agent research systems.

Instead of treating a conversation as the primary unit of state, ResearchSwarm coordinates
framework-agnostic agents through structured tasks, dependencies, artifacts, events, and durable
run state. Phase 2 adds persistent research semantics; Phase 3 adds explicit value estimates,
budget-aware scheduling, and stopping decisions above the small execution runtime.

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

## Dynamic scheduling and budgets (Phase 3)

`PriorityScheduler`: **Which task was assigned the largest static priority?**

`InformationValueScheduler`: **Which ready task currently provides the most expected research
value per unit cost?**

ResearchSwarm does not infer these values. Agents or domain logic supply explicit numeric
`SchedulingSignals`; future policies can use the same interfaces. Scheduling never reads natural
language hypotheses, calls an LLM manager, or changes research confidence. `TaskGraph` still controls
readiness, and `ResearchGraph` still stores epistemic state.

```python
from researchswarm import (
    AgentResult, BudgetLimits, InformationValueScheduler, ResourceUsage,
    SchedulingSignals, Swarm, Task, set_scheduling_signals,
)

class MeasurementAgent:
    async def run(self, task, context):
        return AgentResult(
            output={"measurement": 0.42},
            usage=ResourceUsage(cost=0.12, input_tokens=100, output_tokens=40),
        )

swarm = Swarm(
    "Choose useful measurements",
    scheduler=InformationValueScheduler(),
    budget=BudgetLimits(max_cost=0.5, max_tasks=3),
    max_concurrency=2,
)
swarm.register_agent("measure", MeasurementAgent())
task = Task("Study X", "Measure X against Y", agent_type="measure")
set_scheduling_signals(task, SchedulingSignals(
    expected_information_gain=0.9, uncertainty=0.8, novelty=0.9,
    dependency_value=0.7, estimated_cost=0.2,
))
swarm.add_task(task)
# In an async function: result = await swarm.run()
```

Signals are stored in `task.metadata["researchswarm"]["scheduling"]`; the helpers preserve unrelated
metadata and validate JSON serialization. An optional `hypothesis_id=` argument on
`set_scheduling_signals` stores an explicit association alongside the estimates. It does not create
research edges or infer uncertainty. `get_scheduling_signals(task)` returns `None` for absent signals;
malformed explicit signal data raises a validation error.

The exact information-value formula is:

```text
score = (expected_information_gain * uncertainty * novelty * dependency_value)
        / max(estimated_cost, epsilon) ** alpha
```

Defaults are `alpha=1.0` and `epsilon=1e-9`. Ranking is descending score, descending task priority,
ascending creation time, then ascending task ID. With `fallback_to_priority=True` (the default),
a task without signals uses its static priority as its score, including in mixed queues. With
`False`, it uses default signals (all value factors 0.5, cost 1). Zero-cost estimates are safe.

Passing `budget=BudgetLimits(...)` to `Swarm` automatically applies `BudgetAwareScheduler` around the
chosen policy and enables budget-exhausted/no-affordable-work stop conditions. The wrapper walks the
policy's ranking and reserves **combined** estimated cost, tokens, and task slots across each batch.
It skips an unaffordable candidate and considers cheaper ones. Selected concurrent tasks must each
fit the remaining time estimate; their durations are not summed. Skipping leaves tasks ready.
`BudgetAwareScheduler(policy, ledger)` is also usable independently; standalone callers update that
ledger themselves. With `Swarm`, use its `budget=` argument for durable accounting and stop control.

A missing signal record reserves default cost 1. Unknown token/time estimates reserve zero, so these
are admission estimates, not guarantees. Actual usage may exceed estimates, including across a
concurrent batch. Limits are checked between rounds; active tasks finish normally. Zero remaining
in any configured dimension triggers a stop, even when a task estimates zero in that dimension.
Unconfigured limits (`None`) are unbounded. Cost is an application-defined scalar, not necessarily
currency. Task limits count successfully completed tasks; failed attempts do not count as completed.

`AgentResult.usage` is optional and backward compatible. `ResourceUsage` records cost, input/output
tokens, compute seconds, and custom numeric counters. Usage is written to `task.completed.payload.usage`
and to a small namespaced field on the saved task for recovery. No separate usage event is needed.
Missing usage remains unknown and contributes zero reported cost/tokens; it still counts as a
completed task. Failed/interrupted agents that never return usage cannot be charged from this trace.
Compute seconds and custom counters are retained in the trace; wall budget uses actual elapsed time
since the run's original start, **including time offline**. Wall time is not the sum of compute times.

`BudgetLedger.record(task_id, usage)` deduplicates completion records. `snapshot()`, `can_afford(signals)`,
and `exhausted()` expose actual totals and remaining capacity. Cost arithmetic uses decimal values
constructed from numeric strings, avoiding ordinary binary accumulation errors. Usage totals may
exceed limits; remaining amounts clamp to zero.

`Swarm.load(...)` restores saved budget limits and reconstructs usage from persisted completion
events, filling gaps from completed task records. This covers interruption between saving a completed
task and emitting its completion event. Replayed records do not double-count tasks. Supply `budget=`
on load to explicitly replace limits; `BudgetLimits()` removes all limits while preserving used totals.
Policies, agents, and custom stop-condition objects are not serialized and must be supplied again.

Existing third-party `SchedulerProtocol.choose(ready_tasks, available_slots)` implementations continue
to work. An optional `ContextualSchedulerProtocol.choose_with_context(...)` receives a
`SchedulingContext` with run ID, budget snapshot, completed IDs, and a numeric summary. The executor
currently supplies ready/completed task counts; external controllers can supply richer numeric
summaries. Budget wrappers ask the underlying policy for a full ordering before filtering. Custom
policies should return up to the requested count and must select unique members of the ready set.

Stopping is composable through `StopConditionProtocol.should_stop(OrchestrationState) -> StopDecision`.
Built-ins include `BudgetExhaustedStop`, `NoAffordableTasksStop`, `MaxTasksStop(n)`, and
`AnyStopCondition([...])`. Pass `stop_condition=` to `Swarm`; budget conditions are added automatically
when a budget is configured. `MaxTasksStop` also caps batch slots so concurrency does not overshoot its
completion count. Conditions are evaluated between rounds when ready work remains.

An intentional stop records `metadata["researchswarm"]["stop_reason"]`, emits `run.stop_requested`,
and finishes the run as `COMPLETED`. This means the configured orchestration objective has stopped;
remaining tasks retain their pending/ready state. Inspect `completed_count` and the stop reason to
distinguish it from completing the entire DAG. Existing agent failures still produce `FAILED` and
explicit cancellation still produces `CANCELLED`. Completed/stopped runs remain terminal, consistent
with the existing resume contract; resume applies to interrupted nonterminal runs.

Two scheduling events per round record `scheduler.round_started` and `scheduler.tasks_selected`.
The latter contains selected IDs, candidate priorities/timestamps, the policy name, and serializable
`TaskScore` explanations when supported. Budgeted rounds also include candidate estimates. Built-in
information-value scores identify priority fallback explicitly. `trace_scores=True` additionally
emits individual `scheduler.task_scored` events. Arbitrary legacy policies supply their selection
order, not an invented explanation of their private logic. Budget snapshots emit `budget.updated`
at startup and after each batch; a budget-triggered stop emits `budget.exhausted` and
`run.stop_requested`. No learned policies, bandits, or automated experiment selection are included.

Run [`examples/information_value_scheduler.py`](examples/information_value_scheduler.py) to see
scores, estimated and actual costs, remaining budget, and a two-slot selection trace without API keys.

## Portable traces and replay (Phase 4)

- **Storage** is live durable state used to run ResearchSwarm.
- **RunTrace** is a portable representation of what happened.
- **Replay** deterministically reconstructs recorded event state without invoking agents.
- **SwarmBench** is a future package that will evaluate and compare traces.

```python
from researchswarm import RunTrace, TraceValidator, ReplayEngine, verify_replay

# Inside an async function, after running a swarm:
trace = await result.trace()
trace.to_json("run.json")
# Or: await result.export_trace("run.json")

loaded = RunTrace.load("run.json")  # No database, original agents, or providers needed.
validation = TraceValidator.validate(loaded)
validation.raise_for_errors()
state = ReplayEngine.replay(loaded)
assert state.run_status == result.status
verify_replay(loaded).raise_for_errors()
print(loaded.manifest())
```

The public schema is `researchswarm.trace.v1`; unsupported versions raise
`UnsupportedTraceVersionError`. Explicit task, artifact and research entity records preserve IDs,
relationships, timestamps, metadata, outcomes and reported resource usage. Events are ordered by
sequence number. Exports also support `CREATED` and `RUNNING` runs without requiring a completion event:

```python
trace = await RunTrace.from_storage(run_id, storage)
trace.to_directory("exported-run")  # New directory: trace.json, events.jsonl, manifest.json, artifacts/
```

Artifact data is excluded by default. Control task content separately:

```python
from researchswarm import TraceExportOptions

trace = await result.trace(TraceExportOptions(
    include_task_inputs=False,
    include_task_outputs=False,
    include_artifact_data=False,
))
```

These flags exclude the named fields, not arbitrary metadata or event payloads. Traces may contain
prompts, model responses, datasets, or proprietary research. Review them before sharing. URIs are
preserved without fetching external data. Optional artifact exports copy JSON, not binary storage.

Replay derives run/task states, execution order, artifact creation, reported usage, budget snapshots,
stop reasons, and research-event summaries. Full research contents come from the exported snapshot:
ID-only events cannot reconstruct statements or designs. Unknown custom events are retained.
`verify_replay` returns useful diagnostics when stored snapshots differ from recorded event state;
it never fills event gaps from final state to make verification pass. Direct artifact storage writes
without creation events produce warnings.

API export uses the existing storage protocol, which has no cross-record snapshot transaction.
Export a quiescent run when possible. A partial export or a crash between saving state and appending
an event may yield a verification mismatch; the stored data remains inspectable.

`trace.manifest()` gives version, run identity/objective/status, counts, timing, total usage and a
trace fingerprint. `fingerprint_json(...)` and `trace.fingerprints()` provide deterministic SHA-256
comparison hashes; these are not authenticated or tamper-proof provenance. Export timestamps are
excluded from the trace body hash. Factual helpers include `task_dependencies()`, `execution_order()`,
`tasks_by_agent()`, `research_entities_by_type()`, `total_usage()`, `duration()`, `failed_tasks()`, and
`compare_traces(a, b)`. No quality scores or rankings are calculated.

After installing the package, the standard-library CLI supports:

```bash
researchswarm trace export --db researchswarm.db --run-id run_123 --output trace.json
researchswarm trace validate trace.json
researchswarm trace replay trace.json
```

`python -m researchswarm` offers the same commands from a checkout. Export flags include `--directory`,
`--include-artifact-data`, `--exclude-task-inputs`, and `--exclude-task-outputs`. CLI exports read a
consistent SQLite backup without modifying the source database. Validation/replay return exit code
0 for success, 1 for reported validation/verification failures, and 2 for loading/command errors.

See the [v1 interchange contract](docs/trace-format-v1.md) for the JSON field definitions and replay
semantics, and [trace_and_replay.py](examples/trace_and_replay.py) for a deterministic example. An
external library can analyze the JSON with a standard JSON parser; it does not need runtime objects
or the original database. This is the ResearchSwarm-to-SwarmBench boundary, not SwarmBench itself.

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
- [`examples/trace_and_replay.py`](examples/trace_and_replay.py): export, reload, validate, and replay a research run.

Run any example from the repository root after installing the package.

## Non-goals and roadmap

Phase 4 adds a portable trace and replay boundary to Phase 3 scheduling. Semantic deduplication,
automatic conflict detection, confidence updates, automatic experiment design, embeddings, LLM
supervisors, bandits, reinforcement learning, and SwarmBench evaluation remain future work.
Those policies and evaluation tools can build on the relationship:

```text
Research semantics -> ResearchSwarm runtime -> Structured event trace -> SwarmBench
```

## License

MIT
