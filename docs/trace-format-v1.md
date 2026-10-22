# ResearchSwarm trace v1 interchange contract

This JSON format is the boundary for external analysis. Consumers can use a JSON parser without
opening a ResearchSwarm database or constructing its agents, scheduler, or executor. The Python
`researchswarm.trace` API is a convenience over the same document. This is not an evaluation format
that assigns scores or rankings.

## Version and conventions

Every document has `schema_version: "researchswarm.trace.v1"`. Unknown versions raise
`UnsupportedTraceVersionError`; no implicit forward migration is performed. The version describes
the wire contract, independently of `producer_version` (the exporting ResearchSwarm package version).
All fields described below are emitted, including nulls. Readers should preserve unknown custom
metadata/event payloads. The v1 Python loader reads the known envelope fields; it does not promise
to round-trip additional envelope fields invented outside this contract.

IDs are strings, preserved from storage. Timestamps are ISO-8601 strings with explicit UTC offsets;
optional timestamps are JSON null. All numbers must be finite. Object keys are strings. Statuses
are lowercase strings. Arrays of tasks/artifacts/research entities/research edges are sorted by
ID, using lexicographic ordering. Events are sorted by integer `sequence_number`, never timestamp.
A valid event stream starts at 1 and has no gaps or duplicates. An interrupted run contains its
ordered prefix of events and does not need a terminal run event. This does not define a format for
arbitrary middle-of-stream slices.

## Document fields

| Field | Value |
| --- | --- |
| `schema_version` | `researchswarm.trace.v1` |
| `producer_version` | Exporting package version string |
| `exported_at` | Aware timestamp of export |
| `export_options` | Booleans `include_task_inputs`, `include_task_outputs`, `include_artifact_data` |
| `run` | Run record below |
| `tasks` | Array of task records |
| `artifacts` | Array of artifact records |
| `events` | Array of ordered event records |
| `research_entities` | Array of typed entity envelopes |
| `research_edges` | Array of directed relationships |
| `usage` | Usage summary, or null if unavailable |

Run fields: `run_id`, `objective`, `status`, `created_at`, `started_at`, `completed_at`, `metadata`,
`root_task_ids`. Run statuses: `created`, `running`, `completed`, `failed`, `cancelled`. Budget-stopped
runs use `completed` with `metadata.researchswarm.stop_reason`; they may have unfinished tasks.

Task fields: `task_id`, `name`, `description`, `agent_type` (nullable), `dependencies` (ID array),
`status`, `priority`, `created_at`, `started_at`, `completed_at`, `inputs`, `output`, `error` (nullable),
`metadata`, `usage` (nullable). Task statuses: `pending`, `ready`, `running`, `completed`, `failed`,
`cancelled`, `blocked`. Included inputs/outputs are JSON objects. Excluded inputs/outputs are null,
which is different from an included empty object. `metadata` retains explicit scheduling estimates
and other stored fields. Dependencies describe the execution DAG, not research relationships.

Artifact fields: `artifact_id`, `name`, `type`, `producer_task_id` (nullable), `uri` (nullable),
`metadata`, `created_at`, `data_included`, `data`. `data_included=false` requires `data=null`.
`data_included=true` permits any JSON value, including null. External URIs are descriptive references:
export and replay never fetch them. The default export excludes artifact data.

Event fields: `id`, `run_id`, `task_id` (nullable), `event_type`, `timestamp`, `payload`,
`sequence_number`. Payloads are JSON objects. Core names and payloads are retained from the persisted
runtime event stream, including scheduling decisions, budget snapshots, and custom events. A task
completion can contain `payload.usage`. Usage is not inferred from scheduling estimates.

## Research entities

Each envelope has `entity_id`, `entity_type`, `created_at`, `metadata`, and `payload`. The payload
contains only the following type-specific fields; common identity fields are not duplicated there.

| `entity_type` | Payload fields |
| --- | --- |
| `hypothesis` | `statement`, `rationale`, `predictions`, `falsification_criteria`, `status`, `confidence` |
| `claim` | `statement`, `status`, `confidence` |
| `evidence` | `description`, `evidence_type`, `artifact_id`, `source_uri`, `strength` |
| `experiment` | `name`, `description`, `hypothesis_ids`, `design`, `status`, `task_ids` |
| `result` | `experiment_id`, `summary`, `metrics`, `artifact_ids`, `successful` |
| `critique` | `target_id`, `summary`, `severity`, `recommendations` |
| `conflict` | `left_id`, `right_id`, `description`, `importance`, `status` |

Statuses follow the Phase 2 enums: hypothesis (`proposed`, `active`, `supported`, `rejected`,
`inconclusive`); claim (`open`, `supported`, `refuted`, `inconclusive`); experiment (`proposed`, `ready`,
`running`, `completed`, `failed`, `cancelled`); conflict (`open`, `resolved`, `dismissed`). Critique
severities are `info`, `minor`, `major`, `critical`.

Research edge fields: `id`, `source_id`, `target_id`, `relation`, `metadata`, `created_at`. Relations
include `supports`, `contradicts`, `tests`, `produces`, `critiques`, `derived_from`, `refines`, `resolves`;
custom nonempty strings remain valid. Research graphs may contain cycles.

## Usage

Task usage fields are `cost`, `input_tokens`, `output_tokens`, `compute_seconds`, `custom` (a map of
named nonnegative numeric counters). These are reported actual usage, not estimates. Absent usage is
null. Export prefers the independently saved completed-task usage, falling back to completion events
for records lacking that snapshot. Conflicting event/snapshot values are visible to verification.

The summary contains `total_cost`, `input_tokens`, `output_tokens`, `compute_seconds`,
`tasks_with_usage`, `custom`. It sums task-level records, once per task, using decimal accumulation for
floating resources. Missing records contribute zero and do not increase `tasks_with_usage`. An
explicit zero usage record does increase it. `total_usage()` calculates from task records; validation
checks an included declared summary against that calculation. No currency unit is assumed.

## Validation and replay

Loading checks JSON shape, required wire fields, date parsing and enum conversion. Use
`TraceValidator.validate(trace)` for references, duplicate IDs, sequences, task DAG acyclicity,
research payloads, export policy consistency, known states, and basic lifecycle checks. The result
has `valid`, `errors`, `warnings`, and `raise_for_errors()`.

`EventReducer.apply(state, event)` updates an owned accumulator. `ReplayEngine.replay(trace)` consumes
events and never seeds state from final task/run snapshots. Task starts establish execution order;
recovery resets a task to pending, and a later start appears again in the order. Completion events
record usage once per task. Budget events update the latest budget snapshot, without charging usage
again. Stop events record a reason. Unknown events are retained in `uninterpreted_events`.

Research events only reconstruct ID/type/status summaries and explicit edge additions/deletions.
They cannot recreate statements, designs, artifact contents, or other payloads that were not emitted.
Those remain available in the independently exported research snapshot.

`verify_replay(trace)` compares run/task statuses, stop reason, task-level usage and declared summary
with event-derived state. Missing creation events for directly stored artifacts produce warnings.
A storage write without its corresponding event can produce a valid snapshot but a verification
mismatch. Verification reports this instead of silently repairing history. `strict=False` on replay
allows diagnostic inspection of an invalid event stream; normal replay rejects lifecycle/sequence
errors. This is basic consistency checking, not formal verification or automatic event repair.

## Files, privacy and fingerprints

A single JSON file is sufficient to load and analyze a run. Directory export adds `trace.json`,
`events.jsonl`, `manifest.json`, and `artifacts/`. JSONL contains exactly one complete JSON event per
line. Optional artifact JSON copies have SHA-256-derived filenames based on artifact IDs, never
filesystem paths taken from IDs. Binary storage is not copied. A directory export requires a new
directory to avoid leaving sensitive artifacts from an earlier export.

Export flags exclude only their named fields. Metadata, research text, event payloads, and URIs may
still contain prompts, model responses, datasets, or proprietary research. No general redaction or
secret scanner is performed. Review exports before sharing. Excluded fields are not fingerprinted.

`fingerprint_json(value)` hashes canonical UTF-8 JSON using SHA-256. Canonicalization uses sorted
string keys, compact separators, unescaped Unicode, and disallows NaN/infinity; it does not coerce
numeric representations (`1` differs from `1.0`) or normalize Unicode/timestamp offsets. This is
ResearchSwarm's convention, not RFC 8785. Trace body hashing sorts records as above and excludes only
`exported_at`. Producer version and export controls remain part of the fingerprint. Thus repeated
exports of the same stored content and options have the same fingerprint.

`trace.fingerprints()` returns hashes for included task inputs/outputs, artifact JSON data, complete
research entity envelopes, and the trace body. `manifest()` includes the trace fingerprint with
counts, objective, version, status, start/end time and usage. Fingerprints support comparison and
identity; they are not signatures, authenticity checks, or tamper-proof provenance.

## Snapshot and comparison limits

`RunTrace.from_storage` uses only the storage protocols. They do not expose a transaction spanning
all record types, so export a quiescent run for consistent API snapshots. Partial runs are supported,
but concurrent writes or a crash between a state save and an event append can be diagnosed as gaps.
The CLI first takes a SQLite backup from a read-only source connection, then exports the copy. It
never migrates the source database. Even a consistent backup can capture a state/event write gap.

`compare_traces(a, b)` reports b-minus-a counts, cost, tokens, duration and both statuses. Duration is
null for unstarted/running runs; comparison does not invent a duration from export time. Neither
this helper nor the manifest evaluates research quality. SwarmBench remains a future consumer.
