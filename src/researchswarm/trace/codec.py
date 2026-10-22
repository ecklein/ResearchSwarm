"""Explicit v1 wire field mappings. Changes here change the public interchange contract."""

from __future__ import annotations

from typing import Any

from researchswarm.models.event import Event
from researchswarm.models.run import RunStatus
from researchswarm.models.task import TaskStatus
from researchswarm.models.usage import ResourceUsage
from researchswarm.research.edges import ResearchEdge
from researchswarm.trace.common import optional_timestamp, parse_timestamp, timestamp
from researchswarm.trace.models import (
    ArtifactTrace,
    ResearchEntityTrace,
    RunTraceRun,
    TaskTrace,
    UsageSummary,
)

# Freeze the research v1 payload contract rather than blindly exporting future fields.
RESEARCH_FIELDS = {
    "hypothesis": (
        "statement",
        "rationale",
        "predictions",
        "falsification_criteria",
        "status",
        "confidence",
    ),
    "claim": ("statement", "status", "confidence"),
    "evidence": ("description", "evidence_type", "artifact_id", "source_uri", "strength"),
    "experiment": ("name", "description", "hypothesis_ids", "design", "status", "task_ids"),
    "result": ("experiment_id", "summary", "metrics", "artifact_ids", "successful"),
    "critique": ("target_id", "summary", "severity", "recommendations"),
    "conflict": ("left_id", "right_id", "description", "importance", "status"),
}


def _shape(
    data: dict[str, Any],
    *,
    strings: tuple[str, ...] = (),
    optional_strings: tuple[str, ...] = (),
    objects: tuple[str, ...] = (),
    optional_objects: tuple[str, ...] = (),
    id_arrays: tuple[str, ...] = (),
) -> None:
    if not isinstance(data, dict):
        raise ValueError("record must be an object")
    for name in strings:
        if not isinstance(data[name], str):
            raise ValueError(f"{name} must be a string")
    for name in optional_strings:
        if data[name] is not None and not isinstance(data[name], str):
            raise ValueError(f"{name} must be a string or null")
    for name in objects:
        if not isinstance(data[name], dict):
            raise ValueError(f"{name} must be an object")
    for name in optional_objects:
        if data[name] is not None and not isinstance(data[name], dict):
            raise ValueError(f"{name} must be an object or null")
    for name in id_arrays:
        if not isinstance(data[name], list) or any(
            not isinstance(item, str) for item in data[name]
        ):
            raise ValueError(f"{name} must be an array of strings")


def usage_dict(value: ResourceUsage) -> dict[str, Any]:
    return {
        "cost": value.cost,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "compute_seconds": value.compute_seconds,
        "custom": value.custom,
    }


def summary_dict(value: UsageSummary) -> dict[str, Any]:
    return {
        "total_cost": value.total_cost,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "compute_seconds": value.compute_seconds,
        "tasks_with_usage": value.tasks_with_usage,
        "custom": value.custom,
    }


def run_dict(value: RunTraceRun) -> dict[str, Any]:
    return {
        "run_id": value.run_id,
        "objective": value.objective,
        "status": value.status,
        "created_at": timestamp(value.created_at),
        "started_at": timestamp(value.started_at),
        "completed_at": timestamp(value.completed_at),
        "metadata": value.metadata,
        "root_task_ids": list(value.root_task_ids),
    }


def read_run(data: dict[str, Any]) -> RunTraceRun:
    _shape(
        data,
        strings=("run_id", "objective", "status"),
        objects=("metadata",),
        id_arrays=("root_task_ids",),
    )
    return RunTraceRun(
        data["run_id"],
        data["objective"],
        RunStatus(data["status"]),
        parse_timestamp(data["created_at"]),
        optional_timestamp(data["started_at"]),
        optional_timestamp(data["completed_at"]),
        data["metadata"],
        tuple(data["root_task_ids"]),
    )


def task_dict(value: TaskTrace) -> dict[str, Any]:
    return {
        "task_id": value.task_id,
        "name": value.name,
        "description": value.description,
        "agent_type": value.agent_type,
        "dependencies": list(value.dependencies),
        "status": value.status,
        "priority": value.priority,
        "created_at": timestamp(value.created_at),
        "started_at": timestamp(value.started_at),
        "completed_at": timestamp(value.completed_at),
        "inputs": value.inputs,
        "output": value.output,
        "error": value.error,
        "metadata": value.metadata,
        "usage": usage_dict(value.usage) if value.usage else None,
    }


def read_task(data: dict[str, Any]) -> TaskTrace:
    _shape(
        data,
        strings=("task_id", "name", "description", "status"),
        optional_strings=("agent_type", "error"),
        objects=("metadata",),
        optional_objects=("inputs", "output", "usage"),
        id_arrays=("dependencies",),
    )
    return TaskTrace(
        data["task_id"],
        data["name"],
        data["description"],
        data["agent_type"],
        tuple(data["dependencies"]),
        TaskStatus(data["status"]),
        data["priority"],
        parse_timestamp(data["created_at"]),
        optional_timestamp(data["started_at"]),
        optional_timestamp(data["completed_at"]),
        data["inputs"],
        data["output"],
        data["error"],
        data["metadata"],
        ResourceUsage(**data["usage"]) if data["usage"] is not None else None,
    )


def artifact_dict(value: ArtifactTrace) -> dict[str, Any]:
    return {
        "artifact_id": value.artifact_id,
        "name": value.name,
        "type": value.type,
        "producer_task_id": value.producer_task_id,
        "uri": value.uri,
        "metadata": value.metadata,
        "created_at": timestamp(value.created_at),
        "data_included": value.data_included,
        "data": value.data,
    }


def read_artifact(data: dict[str, Any]) -> ArtifactTrace:
    _shape(
        data,
        strings=("artifact_id", "name", "type"),
        optional_strings=("producer_task_id", "uri"),
        objects=("metadata",),
    )
    return ArtifactTrace(
        data["artifact_id"],
        data["name"],
        data["type"],
        data["producer_task_id"],
        data["uri"],
        data["metadata"],
        parse_timestamp(data["created_at"]),
        data["data_included"],
        data["data"],
    )


def entity_dict(value: ResearchEntityTrace) -> dict[str, Any]:
    return {
        "entity_id": value.entity_id,
        "entity_type": value.entity_type,
        "created_at": timestamp(value.created_at),
        "metadata": value.metadata,
        "payload": value.payload,
    }


def read_entity(data: dict[str, Any]) -> ResearchEntityTrace:
    _shape(data, strings=("entity_id", "entity_type"), objects=("metadata", "payload"))
    return ResearchEntityTrace(
        data["entity_id"],
        data["entity_type"],
        parse_timestamp(data["created_at"]),
        data["metadata"],
        data["payload"],
    )


def event_dict(value: Event) -> dict[str, Any]:
    return {
        "id": value.id,
        "run_id": value.run_id,
        "task_id": value.task_id,
        "event_type": value.event_type,
        "timestamp": timestamp(value.timestamp),
        "payload": value.payload,
        "sequence_number": value.sequence_number,
    }


def read_event(data: dict[str, Any]) -> Event:
    _shape(
        data,
        strings=("id", "run_id", "event_type"),
        optional_strings=("task_id",),
        objects=("payload",),
    )
    return Event(
        run_id=data["run_id"],
        event_type=data["event_type"],
        task_id=data["task_id"],
        payload=data["payload"],
        id=data["id"],
        timestamp=parse_timestamp(data["timestamp"]),
        sequence_number=data["sequence_number"],
    )


def edge_dict(value: ResearchEdge) -> dict[str, Any]:
    return {
        "id": value.id,
        "source_id": value.source_id,
        "target_id": value.target_id,
        "relation": value.relation,
        "created_at": timestamp(value.created_at),
        "metadata": value.metadata,
    }


def read_edge(data: dict[str, Any]) -> ResearchEdge:
    _shape(data, strings=("id", "source_id", "target_id", "relation"), objects=("metadata",))
    return ResearchEdge(
        data["source_id"],
        data["target_id"],
        data["relation"],
        data["metadata"],
        parse_timestamp(data["created_at"]),
        data["id"],
    )
