"""SQLite persistence using only the Python standard library."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from researchswarm.exceptions import (
    ArtifactNotFoundError,
    RunNotFoundError,
    StorageError,
    TaskNotFoundError,
)
from researchswarm.models import Artifact, Event, Run, Task
from researchswarm.serialization import (
    artifact_from_dict,
    ensure_json,
    event_from_dict,
    run_from_dict,
    task_from_dict,
    to_dict,
)

_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    metadata_json TEXT NOT NULL,
    root_task_ids_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    agent_type TEXT,
    status TEXT NOT NULL,
    priority REAL NOT NULL,
    inputs_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    producer_task_id TEXT,
    name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    data_json TEXT,
    uri TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id TEXT,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sequence_number INTEGER NOT NULL,
    UNIQUE (run_id, sequence_number)
);
CREATE INDEX IF NOT EXISTS idx_tasks_run_status ON tasks(run_id, status);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events(run_id, sequence_number);
"""


class SQLiteStorage:
    """Durable SQLite backend with immediate transactional writes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.executescript(_SCHEMA)
            self._connection.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"could not initialize SQLite storage: {exc}") from exc
        self._lock = asyncio.Lock()
        self._closed = False

    async def save_run(self, run: Run) -> None:
        data = to_dict(run)
        async with self._lock:
            self._execute(
                """INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET objective=excluded.objective,
                status=excluded.status, started_at=excluded.started_at,
                completed_at=excluded.completed_at, metadata_json=excluded.metadata_json,
                root_task_ids_json=excluded.root_task_ids_json""",
                (
                    data["id"], data["objective"], data["status"], data["created_at"],
                    data["started_at"], data["completed_at"], _json(data["metadata"]),
                    _json(data["root_task_ids"]),
                ),
            )
            self._commit()

    async def get_run(self, run_id: str) -> Run:
        async with self._lock:
            row = self._fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
            if row is None:
                raise RunNotFoundError(f"run not found: {run_id}")
            return run_from_dict(_run_data(row))

    async def save_task(self, run_id: str, task: Task) -> None:
        data = to_dict(task)
        async with self._lock:
            if self._fetchone("SELECT id FROM runs WHERE id = ?", (run_id,)) is None:
                raise RunNotFoundError(f"run not found: {run_id}")
            self._execute(
                """INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                description=excluded.description, agent_type=excluded.agent_type,
                status=excluded.status, priority=excluded.priority,
                inputs_json=excluded.inputs_json,
                metadata_json=excluded.metadata_json, output_json=excluded.output_json,
                started_at=excluded.started_at, completed_at=excluded.completed_at,
                error=excluded.error""",
                (
                    data["id"], run_id, data["name"], data["description"], data["agent_type"],
                    data["status"], data["priority"], _json(data["inputs"]),
                    _json(data["metadata"]), _json(data["output"]), data["created_at"],
                    data["started_at"], data["completed_at"], data["error"],
                ),
            )
            self._execute("DELETE FROM task_dependencies WHERE task_id = ?", (task.id,))
            for dependency in task.dependencies:
                self._execute(
                    "INSERT INTO task_dependencies VALUES (?, ?)", (task.id, dependency)
                )
            self._commit()

    async def get_task(self, run_id: str, task_id: str) -> Task:
        async with self._lock:
            row = self._fetchone(
                "SELECT * FROM tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
            )
            if row is None:
                raise TaskNotFoundError(f"task not found: {task_id}")
            return task_from_dict(self._task_data(row))

    async def list_tasks(self, run_id: str) -> list[Task]:
        async with self._lock:
            self._require_run(run_id)
            rows = self._fetchall(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            )
            return [task_from_dict(self._task_data(row)) for row in rows]

    async def save_artifact(self, run_id: str, artifact: Artifact) -> None:
        data = to_dict(artifact)
        async with self._lock:
            self._require_run(run_id)
            self._execute(
                """INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET producer_task_id=excluded.producer_task_id,
                name=excluded.name, artifact_type=excluded.artifact_type,
                data_json=excluded.data_json, uri=excluded.uri,
                metadata_json=excluded.metadata_json""",
                (
                    data["id"], run_id, data["producer_task_id"], data["name"],
                    data["artifact_type"],
                    _json(data["data"]) if data["data"] is not None else None,
                    data["uri"], _json(data["metadata"]), data["created_at"],
                ),
            )
            self._commit()

    async def get_artifact(self, run_id: str, artifact_id: str) -> Artifact:
        async with self._lock:
            row = self._fetchone(
                "SELECT * FROM artifacts WHERE run_id = ? AND id = ?", (run_id, artifact_id)
            )
            if row is None:
                raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
            return artifact_from_dict(_artifact_data(row))

    async def list_artifacts(self, run_id: str) -> list[Artifact]:
        async with self._lock:
            self._require_run(run_id)
            rows = self._fetchall(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            )
            return [artifact_from_dict(_artifact_data(row)) for row in rows]

    async def append_event(self, event: Event) -> Event:
        data = to_dict(event)
        async with self._lock:
            self._require_run(event.run_id)
            row = self._fetchone(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS seq FROM events WHERE run_id = ?",
                (event.run_id,),
            )
            assert row is not None
            sequence = int(row["seq"])
            stored = replace(event, sequence_number=sequence)
            self._execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    data["id"], data["run_id"], data["task_id"], data["event_type"],
                    data["timestamp"], _json(data["payload"]), sequence,
                ),
            )
            self._commit()
            return stored

    async def list_events(self, run_id: str) -> list[Event]:
        async with self._lock:
            self._require_run(run_id)
            rows = self._fetchall(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence_number", (run_id,)
            )
            return [event_from_dict(_event_data(row)) for row in rows]

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _task_data(self, row: sqlite3.Row) -> dict[str, Any]:
        dependencies = self._fetchall(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? "
            "ORDER BY depends_on_task_id",
            (row["id"],),
        )
        return {
            "id": row["id"], "name": row["name"], "description": row["description"],
            "agent_type": row["agent_type"], "status": row["status"],
            "priority": row["priority"], "inputs": json.loads(row["inputs_json"]),
            "dependencies": [item["depends_on_task_id"] for item in dependencies],
            "metadata": json.loads(row["metadata_json"]),
            "output": json.loads(row["output_json"]), "created_at": row["created_at"],
            "started_at": row["started_at"], "completed_at": row["completed_at"],
            "error": row["error"],
        }

    def _require_run(self, run_id: str) -> None:
        if self._fetchone("SELECT id FROM runs WHERE id = ?", (run_id,)) is None:
            raise RunNotFoundError(f"run not found: {run_id}")

    def _execute(self, sql: str, parameters: tuple[Any, ...]) -> sqlite3.Cursor:
        try:
            return self._connection.execute(sql, parameters)
        except sqlite3.Error as exc:
            raise StorageError(f"SQLite operation failed: {exc}") from exc

    def _fetchone(self, sql: str, parameters: tuple[Any, ...]) -> sqlite3.Row | None:
        return cast(sqlite3.Row | None, self._execute(sql, parameters).fetchone())

    def _fetchall(self, sql: str, parameters: tuple[Any, ...]) -> list[sqlite3.Row]:
        return self._execute(sql, parameters).fetchall()

    def _commit(self) -> None:
        try:
            self._connection.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"SQLite commit failed: {exc}") from exc


def _json(value: Any) -> str:
    return json.dumps(ensure_json(value), separators=(",", ":"), sort_keys=True, allow_nan=False)


def _run_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "objective": row["objective"], "status": row["status"],
        "created_at": row["created_at"], "started_at": row["started_at"],
        "completed_at": row["completed_at"], "metadata": json.loads(row["metadata_json"]),
        "root_task_ids": json.loads(row["root_task_ids_json"]),
    }


def _artifact_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "artifact_type": row["artifact_type"],
        "data": json.loads(row["data_json"]) if row["data_json"] is not None else None,
        "uri": row["uri"], "producer_task_id": row["producer_task_id"],
        "metadata": json.loads(row["metadata_json"]), "created_at": row["created_at"],
    }


def _event_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "run_id": row["run_id"], "task_id": row["task_id"],
        "event_type": row["event_type"], "timestamp": row["timestamp"],
        "payload": json.loads(row["payload_json"]), "sequence_number": row["sequence_number"],
    }
