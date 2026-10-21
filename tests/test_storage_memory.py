import pytest

from researchswarm import Artifact, Event, MemoryStorage, Run, Task


@pytest.mark.asyncio
async def test_memory_storage_round_trip_and_copy_isolation():
    storage = MemoryStorage()
    run = Run("objective")
    await storage.save_run(run)
    task = Task("task", "")
    await storage.save_task(run.id, task)
    artifact = Artifact("note", "text", data={"x": 1})
    await storage.save_artifact(run.id, artifact)
    first = await storage.append_event(Event(run.id, "x"))
    second = await storage.append_event(Event(run.id, "y"))
    loaded = await storage.get_task(run.id, task.id)
    loaded.name = "mutated"
    assert (await storage.get_task(run.id, task.id)).name == "task"
    assert (await storage.get_artifact(run.id, artifact.id)).data == {"x": 1}
    assert [first.sequence_number, second.sequence_number] == [1, 2]

