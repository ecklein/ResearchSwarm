"""Explicit JSON-safe serialization for ResearchSwarm models."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar, cast

from researchswarm.exceptions import SerializationError
from researchswarm.models import AgentResult, Artifact, Event, Run, RunStatus, Task, TaskStatus
from researchswarm.models.usage import ResourceUsage

T = TypeVar("T")


def ensure_json(value: Any) -> Any:
    """Validate and copy a value through JSON without lossy coercion."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise SerializationError(f"value is not JSON serializable: {exc}") from exc


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


def to_dict(model: Task | Artifact | Run | Event | AgentResult) -> dict[str, Any]:
    """Serialize a supported model to a JSON-compatible dictionary."""
    return cast(dict[str, Any], ensure_json(_value(asdict(model))))


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def task_from_dict(data: dict[str, Any]) -> Task:
    """Deserialize a task."""
    item = dict(data)
    item["status"] = TaskStatus(item["status"])
    item["dependencies"] = tuple(item.get("dependencies", ()))
    item["created_at"] = _datetime(item["created_at"])
    item["started_at"] = _datetime(item.get("started_at"))
    item["completed_at"] = _datetime(item.get("completed_at"))
    return Task(**item)


def artifact_from_dict(data: dict[str, Any]) -> Artifact:
    """Deserialize an artifact."""
    item = dict(data)
    item["created_at"] = _datetime(item["created_at"])
    return Artifact(**item)


def run_from_dict(data: dict[str, Any]) -> Run:
    """Deserialize a run."""
    item = dict(data)
    item["status"] = RunStatus(item["status"])
    item["created_at"] = _datetime(item["created_at"])
    item["started_at"] = _datetime(item.get("started_at"))
    item["completed_at"] = _datetime(item.get("completed_at"))
    return Run(**item)


def event_from_dict(data: dict[str, Any]) -> Event:
    """Deserialize an event."""
    item = dict(data)
    item["timestamp"] = _datetime(item["timestamp"])
    return Event(**item)


def agent_result_from_dict(data: dict[str, Any]) -> AgentResult:
    """Deserialize an agent result."""
    return AgentResult(
        output=data.get("output", {}),
        artifacts=[artifact_from_dict(item) for item in data.get("artifacts", [])],
        metadata=data.get("metadata", {}),
        new_tasks=[task_from_dict(item) for item in data.get("new_tasks", [])],
        usage=ResourceUsage(**data["usage"]) if data.get("usage") is not None else None,
    )
