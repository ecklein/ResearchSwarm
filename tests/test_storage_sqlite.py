import pytest

from researchswarm import Artifact, Event, Run, SQLiteStorage, Task, TaskStatus


@pytest.mark.asyncio
async def test_sqlite_storage_persists_all_records(tmp_path):
    path = tmp_path / "run.db"
    storage = SQLiteStorage(path)
    run = Run("objective", root_task_ids=[])
    await storage.save_run(run)
    first = Task("first", "")
    second = Task("second", "", dependencies=(first.id,))
    await storage.save_task(run.id, first)
    await storage.save_task(run.id, second)
    second.transition_to(TaskStatus.READY)
    await storage.save_task(run.id, second)
    artifact = Artifact("note", "text", data={"x": [1, 2]})
    await storage.save_artifact(run.id, artifact)
    await storage.append_event(Event(run.id, "one"))
    await storage.append_event(Event(run.id, "two"))
    await storage.close()

    reopened = SQLiteStorage(path)
    tasks = await reopened.list_tasks(run.id)
    assert tasks[1].dependencies == (first.id,)
    assert tasks[1].status is TaskStatus.READY
    assert (await reopened.get_artifact(run.id, artifact.id)).data == {"x": [1, 2]}
    assert [event.sequence_number for event in await reopened.list_events(run.id)] == [1, 2]
    await reopened.close()

