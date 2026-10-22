"""Portable run snapshots and deterministic export; no executor is needed to load them."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from researchswarm.models.event import Event
from researchswarm.models.task import TaskStatus
from researchswarm.models.usage import ResourceUsage
from researchswarm.research.edges import ResearchEdge
from researchswarm.research.serialization import entity_to_dict
from researchswarm.trace import codec
from researchswarm.trace.common import (
    SCHEMA_VERSION,
    TraceValidationError,
    canonical_json,
    check_version,
    fingerprint_json,
    parse_timestamp,
    timestamp,
)
from researchswarm.trace.models import (
    ArtifactTrace,
    ResearchEntityTrace,
    RunTraceRun,
    TaskTrace,
    TraceExportOptions,
    UsageSummary,
)
from researchswarm.utils import utc_now

if TYPE_CHECKING:
    from researchswarm.storage.base import StorageProtocol


def _version() -> str:
    try:
        return version("researchswarm")
    except PackageNotFoundError:
        return "0.1.0"


def _event_key(event: Event) -> int:
    if type(event.sequence_number) is not int or event.sequence_number < 1:
        raise TraceValidationError(f"event {event.id} needs a positive integer sequence_number")
    return event.sequence_number


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise TraceValidationError(f"duplicate JSON key: {key}")
        data[key] = value
    return data


@dataclass(frozen=True)
class RunTrace:
    schema_version: str
    run: RunTraceRun
    tasks: tuple[TaskTrace, ...]
    artifacts: tuple[ArtifactTrace, ...]
    events: tuple[Event, ...]
    research_entities: tuple[ResearchEntityTrace, ...]
    research_edges: tuple[ResearchEdge, ...]
    usage: UsageSummary | None
    exported_at: datetime
    producer_version: str = "0.1.0"
    export_options: TraceExportOptions = TraceExportOptions()

    @classmethod
    async def from_storage(
        cls,
        run_id: str,
        storage: StorageProtocol,
        options: TraceExportOptions | None = None,
    ) -> RunTrace:
        """Read a complete or partial snapshot without changing storage.

        StorageProtocol has no cross-record snapshot transaction. Export a quiescent
        run for a consistent snapshot; verification reports state/event write gaps.
        """
        from researchswarm.storage.base import ResearchStorageProtocol

        options = options or TraceExportOptions()
        run = await storage.get_run(run_id)
        stored_tasks = await storage.list_tasks(run_id)
        stored_artifacts = await storage.list_artifacts(run_id)
        entities: list[ResearchEntityTrace] = []
        edges: list[ResearchEdge] = []
        if isinstance(storage, ResearchStorageProtocol):
            edges = await storage.list_research_edges(run_id)
            for entity in await storage.list_research_entities(run_id):
                data = entity_to_dict(entity)
                kind = data["entity_type"]
                entities.append(
                    ResearchEntityTrace(
                        entity.id,
                        kind,
                        entity.created_at,
                        data["metadata"],
                        {key: data[key] for key in codec.RESEARCH_FIELDS[kind]},
                    )
                )
        events = tuple(sorted(await storage.list_events(run_id), key=_event_key))
        reported: dict[str, ResourceUsage | None] = {}
        for event in events:
            if event.event_type == "task.completed" and event.task_id not in reported:
                raw = event.payload.get("usage")
                if event.task_id is not None:
                    reported[event.task_id] = ResourceUsage(**raw) if raw is not None else None
        tasks = []
        for task in stored_tasks:
            usage = reported.get(task.id)
            if task.status is TaskStatus.COMPLETED:
                scoped = task.metadata.get("researchswarm", {})
                raw = scoped.get("usage") if isinstance(scoped, dict) else None
                if raw is not None:
                    # Preserve the independently saved usage snapshot so verification
                    # can detect disagreements with the event stream.
                    usage = ResourceUsage(**raw)
            tasks.append(
                TaskTrace(
                    task.id,
                    task.name,
                    task.description,
                    task.agent_type,
                    task.dependencies,
                    task.status,
                    task.priority,
                    task.created_at,
                    task.started_at,
                    task.completed_at,
                    task.inputs if options.include_task_inputs else None,
                    task.output if options.include_task_outputs else None,
                    task.error,
                    task.metadata,
                    usage,
                )
            )
        artifacts = tuple(
            ArtifactTrace(
                item.id,
                item.name,
                item.artifact_type,
                item.producer_task_id,
                item.uri,
                item.metadata,
                item.created_at,
                options.include_artifact_data,
                item.data if options.include_artifact_data else None,
            )
            for item in stored_artifacts
        )
        trace = cls(
            SCHEMA_VERSION,
            RunTraceRun(
                run.id,
                run.objective,
                run.status,
                run.created_at,
                run.started_at,
                run.completed_at,
                run.metadata,
                tuple(run.root_task_ids),
            ),
            tuple(tasks),
            artifacts,
            events,
            tuple(entities),
            tuple(edges),
            UsageSummary.aggregate(task.usage for task in tasks),
            utc_now(),
            _version(),
            options,
        )
        # Detach nested values and apply the same explicit wire contract as file loading.
        return cls.from_dict(trace.to_dict())

    def to_dict(self) -> dict[str, Any]:
        check_version(self.schema_version)
        data = {
            "schema_version": self.schema_version,
            "producer_version": self.producer_version,
            "exported_at": timestamp(self.exported_at),
            "export_options": {
                "include_task_inputs": self.export_options.include_task_inputs,
                "include_task_outputs": self.export_options.include_task_outputs,
                "include_artifact_data": self.export_options.include_artifact_data,
            },
            "run": codec.run_dict(self.run),
            "tasks": [
                codec.task_dict(item) for item in sorted(self.tasks, key=lambda item: item.task_id)
            ],
            "artifacts": [
                codec.artifact_dict(item)
                for item in sorted(self.artifacts, key=lambda item: item.artifact_id)
            ],
            "events": [codec.event_dict(event) for event in sorted(self.events, key=_event_key)],
            "research_entities": [
                codec.entity_dict(item)
                for item in sorted(self.research_entities, key=lambda item: item.entity_id)
            ],
            "research_edges": [
                codec.edge_dict(edge)
                for edge in sorted(self.research_edges, key=lambda edge: edge.id)
            ],
            "usage": codec.summary_dict(self.usage) if self.usage is not None else None,
        }
        # Validate/copy without serializing private runtime attributes or dataclass internals.
        result: dict[str, Any] = json.loads(canonical_json(data))
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunTrace:
        check_version(data.get("schema_version", ""))
        try:
            data = json.loads(canonical_json(data))
            for name in ("tasks", "artifacts", "events", "research_entities", "research_edges"):
                if not isinstance(data[name], list):
                    raise ValueError(f"{name} must be an array")
            return cls(
                data["schema_version"],
                codec.read_run(data["run"]),
                tuple(codec.read_task(item) for item in data["tasks"]),
                tuple(codec.read_artifact(item) for item in data["artifacts"]),
                tuple(codec.read_event(item) for item in data["events"]),
                tuple(codec.read_entity(item) for item in data["research_entities"]),
                tuple(codec.read_edge(item) for item in data["research_edges"]),
                UsageSummary(**data["usage"]) if data["usage"] is not None else None,
                parse_timestamp(data["exported_at"]),
                data["producer_version"],
                TraceExportOptions(**data["export_options"]),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise TraceValidationError(f"invalid trace document: {exc}") from exc

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
            )
            + "\n",
            encoding="utf-8",
        )

    def to_directory(self, path: str | Path) -> None:
        """Export to a new directory; never leave stale artifact data in an old export."""
        target = Path(path)
        target.mkdir(parents=True, exist_ok=False)
        (target / "artifacts").mkdir()
        self.to_json(target / "trace.json")
        (target / "events.jsonl").write_text(
            "".join(
                canonical_json(codec.event_dict(event)) + "\n"
                for event in sorted(self.events, key=_event_key)
            ),
            encoding="utf-8",
        )
        (target / "manifest.json").write_text(
            json.dumps(self.manifest(), sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        for artifact in self.artifacts:
            if artifact.data_included:
                # IDs are untrusted strings, never filesystem paths.
                filename = fingerprint_json(artifact.artifact_id) + ".json"
                (target / "artifacts" / filename).write_text(
                    canonical_json(codec.artifact_dict(artifact)) + "\n",
                    encoding="utf-8",
                )

    @classmethod
    def load(cls, path: str | Path) -> RunTrace:
        source = Path(path)
        if source.is_dir():
            source = source / "trace.json"
        try:
            data = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_object)
            if not isinstance(data, dict):
                raise ValueError("trace document must be a JSON object")
            return cls.from_dict(data)
        except (ValueError, TypeError) as exc:
            raise TraceValidationError(f"invalid trace JSON: {exc}") from exc

    def fingerprint(self) -> str:
        body = self.to_dict()
        del body["exported_at"]
        return fingerprint_json(body)

    def fingerprints(self) -> dict[str, Any]:
        """Only included content is hashed; excluded values do not leak through hashes."""
        return {
            "trace_body": self.fingerprint(),
            "task_inputs": {
                task.task_id: fingerprint_json(task.inputs)
                for task in self.tasks
                if task.inputs is not None
            },
            "task_outputs": {
                task.task_id: fingerprint_json(task.output)
                for task in self.tasks
                if task.output is not None
            },
            "artifact_data": {
                artifact.artifact_id: fingerprint_json(artifact.data)
                for artifact in self.artifacts
                if artifact.data_included
            },
            "research_entities": {
                entity.entity_id: fingerprint_json(codec.entity_dict(entity))
                for entity in self.research_entities
            },
        }

    def total_usage(self) -> UsageSummary:
        return UsageSummary.aggregate(task.usage for task in self.tasks)

    def duration(self) -> float | None:
        """Completed wall duration; None for unstarted or still-running runs."""
        if self.run.started_at is None or self.run.completed_at is None:
            return None
        return (self.run.completed_at - self.run.started_at).total_seconds()

    def task_dependencies(self) -> dict[str, tuple[str, ...]]:
        return {task.task_id: task.dependencies for task in self.tasks}

    def execution_order(self) -> tuple[str, ...]:
        return tuple(
            event.task_id
            for event in sorted(self.events, key=_event_key)
            if event.event_type == "task.started" and event.task_id is not None
        )

    def tasks_by_agent(self) -> dict[str | None, tuple[TaskTrace, ...]]:
        groups: dict[str | None, list[TaskTrace]] = defaultdict(list)
        for task in self.tasks:
            groups[task.agent_type].append(task)
        return {key: tuple(values) for key, values in groups.items()}

    def research_entities_by_type(self) -> dict[str, tuple[ResearchEntityTrace, ...]]:
        groups: dict[str, list[ResearchEntityTrace]] = defaultdict(list)
        for entity in self.research_entities:
            groups[entity.entity_type].append(entity)
        return {key: tuple(values) for key, values in groups.items()}

    def failed_tasks(self) -> tuple[TaskTrace, ...]:
        return tuple(task for task in self.tasks if task.status == TaskStatus.FAILED)

    def manifest(self) -> dict[str, Any]:
        return {
            "run_id": self.run.run_id,
            "objective": self.run.objective,
            "schema_version": self.schema_version,
            "researchswarm_version": self.producer_version,
            "started_at": timestamp(self.run.started_at),
            "completed_at": timestamp(self.run.completed_at),
            "run_status": str(self.run.status),
            "task_count": len(self.tasks),
            "event_count": len(self.events),
            "artifact_count": len(self.artifacts),
            "research_entity_count": len(self.research_entities),
            "research_edge_count": len(self.research_edges),
            "total_usage": codec.summary_dict(self.total_usage()),
            "trace_fingerprint": self.fingerprint(),
        }
