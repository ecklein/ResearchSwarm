"""Structural, reference, and basic lifecycle validation with actionable diagnostics."""

from __future__ import annotations

from datetime import datetime
from graphlib import CycleError, TopologicalSorter
from math import isfinite
from typing import Any

from researchswarm.exceptions import SerializationError
from researchswarm.models.run import RunStatus
from researchswarm.models.task import TaskStatus
from researchswarm.research.serialization import entity_from_dict
from researchswarm.trace import codec
from researchswarm.trace.common import (
    TraceValidationError,
    ValidationResult,
    canonical_json,
    check_version,
    timestamp,
)
from researchswarm.trace.replay import ReplayEngine
from researchswarm.trace.trace import RunTrace


class TraceValidator:
    @staticmethod
    def validate(trace: RunTrace) -> ValidationResult:
        check_version(trace.schema_version)
        errors: list[str] = []
        warnings: list[str] = []

        def ids(values: list[Any], label: str) -> set[str]:
            good = [value for value in values if isinstance(value, str) and value]
            if len(good) != len(values):
                errors.append(f"{label} IDs must be nonempty strings")
            if len(good) != len(set(good)):
                errors.append(f"duplicate {label} IDs")
            return set(good)

        def reference(value: Any, known: set[str], label: str) -> None:
            if not isinstance(value, str) or value not in known:
                errors.append(f"{label}: missing reference {value!r}")

        def dates(item: Any, label: str) -> None:
            try:
                if not isinstance(item.created_at, datetime):
                    raise ValueError("created_at must be a datetime")
                timestamp(item.created_at)
                start = getattr(item, "started_at", None)
                end = getattr(item, "completed_at", None)
                timestamp(start)
                timestamp(end)
                if start is not None and start < item.created_at:
                    errors.append(f"{label}: started_at precedes created_at")
                if end is not None and end < (start or item.created_at):
                    errors.append(f"{label}: completed_at precedes creation/start")
            except (TypeError, ValueError, AttributeError) as exc:
                errors.append(f"{label}: invalid timestamp: {exc}")

        task_ids = ids([task.task_id for task in trace.tasks], "task")
        artifact_ids = ids([item.artifact_id for item in trace.artifacts], "artifact")
        entity_ids = ids([item.entity_id for item in trace.research_entities], "research entity")
        ids([edge.id for edge in trace.research_edges], "research edge")
        ids([event.id for event in trace.events], "event")
        ids([trace.run.run_id], "run")
        try:
            if not isinstance(trace.exported_at, datetime):
                raise ValueError("exported_at must be a datetime")
            timestamp(trace.exported_at)
            canonical_json(trace.to_dict())
        except (TypeError, ValueError, AttributeError) as exc:
            errors.append(f"trace is not JSON/timestamp serializable: {exc}")
        except TraceValidationError as exc:
            errors.append(str(exc))
        if not isinstance(trace.producer_version, str) or not trace.producer_version:
            errors.append("producer_version must be a nonempty string")
        dates(trace.run, "run")
        try:
            run_status = RunStatus(trace.run.status)
            if run_status is RunStatus.RUNNING and trace.run.started_at is None:
                errors.append("RUNNING run is missing started_at")
            if (
                run_status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
                and trace.run.completed_at is None
            ):
                errors.append("terminal run is missing completed_at")
        except ValueError:
            errors.append(f"unknown run status: {trace.run.status!r}")
        for root_id in trace.run.root_task_ids:
            reference(root_id, task_ids, "run root task")
        for task in trace.tasks:
            if (
                isinstance(task.priority, bool)
                or not isinstance(task.priority, (float, int))
                or not isfinite(task.priority)
            ):
                errors.append(f"task {task.task_id}: priority must be a finite number")
            dates(task, f"task {task.task_id}")
            try:
                status = TaskStatus(task.status)
                if (
                    status in {TaskStatus.RUNNING, TaskStatus.COMPLETED, TaskStatus.FAILED}
                    and task.started_at is None
                ):
                    errors.append(f"task {task.task_id}: {status} requires started_at")
                if (
                    status
                    in {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                        TaskStatus.BLOCKED,
                    }
                    and task.completed_at is None
                ):
                    errors.append(f"task {task.task_id}: {status} requires completed_at")
            except ValueError:
                errors.append(f"unknown task status for {task.task_id}: {task.status!r}")
            if len(set(task.dependencies)) != len(task.dependencies):
                errors.append(f"task {task.task_id}: duplicate dependencies")
            for dependency in task.dependencies:
                reference(dependency, task_ids, f"task {task.task_id} dependency")
            for name, included in (
                ("inputs", trace.export_options.include_task_inputs),
                ("output", trace.export_options.include_task_outputs),
            ):
                value = getattr(task, name)
                if included and not isinstance(value, dict):
                    errors.append(f"task {task.task_id}: included {name} must be an object")
                if not included and value is not None:
                    errors.append(f"task {task.task_id}: excluded {name} is present")
        try:
            TopologicalSorter({task.task_id: task.dependencies for task in trace.tasks}).prepare()
        except CycleError:
            errors.append("task dependency graph contains a cycle")
        for artifact in trace.artifacts:
            dates(artifact, f"artifact {artifact.artifact_id}")
            if artifact.producer_task_id is not None:
                reference(
                    artifact.producer_task_id, task_ids, f"artifact {artifact.artifact_id} producer"
                )
            if type(artifact.data_included) is not bool:
                errors.append(f"artifact {artifact.artifact_id}: data_included must be boolean")
            if not artifact.data_included and artifact.data is not None:
                errors.append(f"artifact {artifact.artifact_id}: excluded data is present")
            if artifact.data_included and not trace.export_options.include_artifact_data:
                errors.append(f"artifact {artifact.artifact_id}: data violates export options")
        kinds = {item.entity_id: item.entity_type for item in trace.research_entities}
        for entity in trace.research_entities:
            dates(entity, f"research entity {entity.entity_id}")
            if entity.entity_type not in codec.RESEARCH_FIELDS:
                errors.append(f"unknown research entity type: {entity.entity_type}")
                continue
            payload = entity.payload
            if not isinstance(payload, dict) or set(payload) != set(
                codec.RESEARCH_FIELDS[entity.entity_type]
            ):
                errors.append(f"entity {entity.entity_id}: invalid v1 payload fields")
                continue
            try:
                entity_from_dict(
                    {
                        **payload,
                        "id": entity.entity_id,
                        "entity_type": entity.entity_type,
                        "created_at": timestamp(entity.created_at),
                        "metadata": entity.metadata,
                    }
                )
            except (SerializationError, TypeError, ValueError) as exc:
                errors.append(f"entity {entity.entity_id}: {exc}")
                continue
            research_refs: list[tuple[Any, str | None]] = []
            if entity.entity_type == "experiment":
                research_refs.extend((item, "hypothesis") for item in payload["hypothesis_ids"])
                for task_id in payload["task_ids"]:
                    reference(task_id, task_ids, f"experiment {entity.entity_id} task")
            elif entity.entity_type == "result":
                research_refs.append((payload["experiment_id"], "experiment"))
                for artifact_id in payload["artifact_ids"]:
                    reference(artifact_id, artifact_ids, f"result {entity.entity_id} artifact")
            elif entity.entity_type == "evidence" and payload["artifact_id"] is not None:
                reference(
                    payload["artifact_id"], artifact_ids, f"evidence {entity.entity_id} artifact"
                )
            elif entity.entity_type == "critique":
                research_refs.append((payload["target_id"], None))
            elif entity.entity_type == "conflict":
                research_refs.extend((payload[key], None) for key in ("left_id", "right_id"))
            for ref, expected_type in research_refs:
                reference(ref, entity_ids, f"entity {entity.entity_id}")
                if expected_type is not None and kinds.get(ref) != expected_type:
                    errors.append(f"entity {entity.entity_id}: {ref!r} must be a {expected_type}")
        for edge in trace.research_edges:
            dates(edge, f"edge {edge.id}")
            reference(edge.source_id, entity_ids, f"edge {edge.id} source")
            reference(edge.target_id, entity_ids, f"edge {edge.id} target")
        for event in trace.events:
            if event.run_id != trace.run.run_id:
                errors.append(f"event {event.id} belongs to another run")
            if event.task_id is not None:
                reference(event.task_id, task_ids, f"event {event.id} task")
            if not isinstance(event.payload, dict):
                continue
            if event.event_type == "artifact.created":
                reference(
                    event.payload.get("artifact_id"), artifact_ids, f"event {event.id} artifact"
                )
            if event.event_type == "task.blocked":
                reference(
                    event.payload.get("dependency_id"), task_ids, f"event {event.id} dependency"
                )
            if event.event_type == "scheduler.tasks_selected":
                selection = event.payload.get("task_ids", [])
                if not isinstance(selection, list):
                    errors.append(f"event {event.id}: task_ids must be an array")
                else:
                    for task_id in selection:
                        reference(task_id, task_ids, f"event {event.id} selection")
            if event.event_type.startswith("research."):
                if "entity_id" in event.payload:
                    reference(event.payload["entity_id"], entity_ids, f"event {event.id} entity")
                if event.event_type == "research.edge.created":
                    for key in ("source_id", "target_id"):
                        reference(event.payload.get(key), entity_ids, f"event {event.id} {key}")
        state = ReplayEngine.replay(trace, strict=False)
        errors.extend(state.diagnostics)
        if trace.usage is not None and trace.usage != trace.total_usage():
            errors.append("declared usage summary does not match task-level usage")
        if not trace.events:
            warnings.append(
                "trace has no events; stored snapshots cannot be reconstructed from events"
            )
        return ValidationResult(tuple(dict.fromkeys(errors)), tuple(warnings))
