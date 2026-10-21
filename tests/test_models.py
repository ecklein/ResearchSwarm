from datetime import UTC, datetime

import pytest

from researchswarm import (
    Artifact,
    Event,
    InvalidStateTransitionError,
    Run,
    Task,
    TaskStatus,
)
from researchswarm.exceptions import SerializationError
from researchswarm.serialization import (
    artifact_from_dict,
    event_from_dict,
    run_from_dict,
    task_from_dict,
    to_dict,
)


def test_ids_have_readable_unique_prefixes():
    assert Task("a", "a").id.startswith("task_")
    assert Artifact("a", "text").id.startswith("artifact_")
    assert Run("objective").id.startswith("run_")
    assert Event("run_x", "custom").id.startswith("event_")
    assert Task("a", "a").id != Task("a", "a").id


def test_task_state_machine_and_timestamps():
    task = Task("a", "a")
    task.transition_to(TaskStatus.READY)
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.COMPLETED)
    assert task.started_at is not None
    assert task.completed_at is not None
    with pytest.raises(InvalidStateTransitionError):
        task.transition_to(TaskStatus.RUNNING)


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        Task("a", "a", created_at=datetime.now())


@pytest.mark.parametrize(
    ("model", "loader"),
    [
        (Task("a", "a", inputs={"n": 1}), task_from_dict),
        (Artifact("a", "text", data={"ok": True}), artifact_from_dict),
        (Run("objective", metadata={"x": "y"}), run_from_dict),
        (Event("run_x", "custom", payload={"x": 1}), event_from_dict),
    ],
)
def test_serialization_round_trip(model, loader):
    assert loader(to_dict(model)) == model


def test_serialization_rejects_unsupported_values():
    with pytest.raises(SerializationError):
        to_dict(Task("a", "a", inputs={"bad": object()}))


def test_timestamps_are_utc_aware():
    assert Task("a", "a").created_at.tzinfo == UTC
